# thsgui-trader

基于 macOS Accessibility 的同花顺客户端交易适配器。

当前版本默认执行只读查询和订单预览；买入、卖出、撤单等会改变账户状态的操作需要显式打开安全开关。服务通过本地 HTTP 接口提供账户状态、行情、订单预览和受保护的交易操作。策略层通常应调用单请求下单管线，不需要处理临时预览 ID。下单管线支持请求中指定 `A股` 或 `模拟` 板块，板块切换由 Accessibility 语义控件完成。

## 设计目标

这个仓库的目标是把 macOS 原生同花顺客户端封装成一个独立的、只读的交易查询服务。上层调用者不需要了解同花顺窗口的位置、按钮坐标、页面切换方式或 macOS Accessibility 的底层 API，只需要调用稳定的 HTTP 接口即可。

当前采用以下原则：

- 先做查询和预览，再做交易。交易按钮默认关闭，必须经过预览和明确确认。
- 使用 Accessibility 控件语义读取数据，优先使用控件角色、标题、表格和文本，不把截图 OCR 或固定坐标作为主路径。
- GUI 细节集中在网关层，上层接口和数据模型不直接依赖同花顺页面结构。
- 解析和校验独立于系统 API，便于用固定控件树 fixture 做测试。
- 所有服务默认只监听本机 `127.0.0.1`，避免无意中暴露账户数据。

## 总体架构

```text
调用方 / 策略层
        |
    | HTTP 请求
        v
FastAPI API 层
src/thsgui_trader/api.py
        |
        | 调用统一网关方法
        v
TonghuashunGuiGateway
src/thsgui_trader/gateways/tonghuashun.py
        |
        | 获取窗口、读取控件、切换只读页面
        v
MacAccessibilityClient
src/thsgui_trader/accessibility/client.py
        |
        | macOS ApplicationServices AXUIElement
        v
同花顺 macOS 客户端
```

数据返回方向如下：

```text
同花顺控件树
    -> AXNode 内存结构
    -> 页面表格 / 资金字段
    -> 解析器
    -> Pydantic 数据模型
    -> FastAPI JSON 响应
```

## 代码分层

### `api.py`：HTTP 接口层

`create_app()` 创建 FastAPI 应用，并把每个 URL 映射到网关方法：

```text
/api/v1/funds       -> get_account_funds()
/api/v1/positions   -> get_positions()
/api/v1/orders      -> get_orders()
/api/v1/trades      -> get_trades()
/api/v1/fund-details -> get_fund_details()
```

API 层只负责三件事：接收请求参数、调用网关、把预期的网关错误转换成 HTTP 503。它不解析控件、不查找按钮，也不包含同花顺字段名称。

### `gateways/base.py`：统一网关契约

`TraderGateway` 是协议接口，定义所有交易客户端适配器需要提供的只读方法：

```text
get_connection_status()
get_account_funds()
get_positions()
get_orders()
get_trades()
get_fund_details(start_date, end_date)
```

以后如果接入其他客户端，可以新增新的网关实现，而不改变 API 层和数据模型。当前真正的实现是 `TonghuashunGuiGateway`。

### `gateways/tonghuashun.py`：同花顺适配层

这个类是“页面操作”和“业务数据”之间的边界：

- 创建 Accessibility 客户端。
- 获取同花顺主窗口控件树。
- 查找“持仓”“委托”“成交”“资金明细”等语义按钮。
- 查询页面通过 `AXPress` 切换；买入、卖出和撤单控件只有在安全开关和确认条件都满足时才允许调用。
- 将当前页面交给对应解析器。

上层不会看到 AX 元素指针、控件索引或同花顺内部 UI 结构。

### `accessibility/client.py`：macOS Accessibility 封装

这一层通过 `ctypes` 调用系统的 `ApplicationServices.framework`：

1. 按 Bundle ID 查找正在运行的同花顺进程。
2. 用进程 ID 创建 `AXUIElement` 应用对象。
3. 获取主窗口，并递归读取控件属性。
4. 把原生对象转换成 Python 的 `AXNode`。
5. 对页面按钮执行有限的 `AXPress`，用于切换查询页、选择报价档位、提交第一层委托和撤单。
6. 对暴露为可写文本框的控件先聚焦控件，再通过系统键盘事件执行替换输入，并在每次输入后重新读取验证。只写 `AXValue` 不会触发同花顺代码输入框的行情刷新；本客户端使用真实键盘 `Tab` 让代码框失焦刷新行情，不能发送回车，因为当数量框残留数值时回车会被同花顺解释为提交委托。
7. 交易表单在一次控件树快照中定位价格框、数量框和提交按钮；输入后只读取对应控件的实时 `AXValue`，不重复扫描整棵窗口。盘口刷新仍使用新的快照确认数据已经出现，避免为了提速而提交旧价格或旧数量。

`AXNode` 保存控件角色、标题、值、描述、帮助文本、启用状态和子节点。解析器只面对这个 Python 数据结构，因此不需要直接依赖 macOS 框架。

### `parsers/`：页面数据解析层

解析器只处理已经读取到的控件树：

- `common.py`：表格抽取、文本清洗、数字转换。
- `funds.py`：把“总资产”“总市值”等标签映射为资金模型字段。
- `positions.py`：按表头或当前客户端稳定的列顺序解析持仓。
- `orders.py`：解析委托编号、方向、价格、数量和状态。
- `trades.py`：解析成交编号、成交价格、数量、金额和手续费。

解析器不调用 GUI，不切换页面，也不返回 HTTP 响应。这样可以使用不依赖真实同花顺窗口的 fixture 测试。

### `models.py`：统一数据模型

Pydantic 模型定义服务对外的字段格式，包括：

- `ConnectionStatus`：连接状态、进程 ID、账户模式。
- `AccountFunds`：资产、资金、盈亏字段。
- `Position`：代码、名称、数量、成本价、最新价和市值。
- `Order`：委托编号、方向、价格、数量、状态。
- `Trade`：成交编号、价格、数量、金额和手续费。
- `FundDetail`：资金明细类型、金额、余额和备注。

模型的作用是把同花顺页面上的中文字段转换成稳定的程序字段，避免调用方依赖 GUI 原始文本。

### `errors.py`：错误边界

- `AccessibilityUnavailable`：同花顺未运行、无法创建 Accessibility 对象或控件操作失败。
- `DataValidationError`：页面存在，但没有找到预期表格或数据结构不完整。
- `GatewayError`：所有可预期的网关错误基类。

API 层会把这些错误转换为 503，表示服务仍然运行，但当前无法可靠地从客户端读取数据。

## 各功能的实现流程

### 连接状态：`GET /api/v1/connection`

1. 按 `cn.com.10jqka.macstock` 查找同花顺进程。
2. 读取主窗口控件树。
3. 搜索控件标题、文本和帮助文本。
4. 出现“已连接”时返回 `connected: true`。
5. 网关主动选择板块后，以最近一次选择的板块填充 `account_mode`（`A股` 或 `模拟`）。当前客户端没有可靠的 selected Accessibility 属性，交易前会再次按请求中的板块选择按钮，避免用户手动切换后上下文错误。

连接状态查询不会切换页面，也不会修改同花顺数据。

### 资金：`GET /api/v1/funds`

1. 读取当前主窗口。
2. 在表格行中寻找“总资产”“总市值”“总盈亏”“当日盈亏”“资金余额”“可取金额”“可用金额”。
3. 清除千分位、百分号和无效占位符。
4. 转换成浮点数并生成 `AccountFunds`。
5. 如果总资产与市值、资金余额的差异超过容差，加入 `warnings`，但不擅自修改数据。

### 持仓：`GET /api/v1/positions`

1. 通过 Accessibility 按钮切换到“持仓”页。
2. 等待客户端刷新后重新读取控件树。
3. 优先按表头匹配字段。
4. 对当前版本中表头位于表格旁边、但数据列顺序稳定的情况，使用受限的 15 列位置映射。
5. 对每一行校验股票代码包含数字，再生成 `Position`。
∂
### 委托和成交：`GET /api/v1/orders`、`GET /api/v1/trades`

1. 通过语义按钮切换到“委托”或“成交”页。
2. 在表格中寻找对应中文表头。
3. 解析价格、数量、状态、时间和金额字段。
4. 如果页面当前没有有效记录或客户端没有暴露表头，返回空数组；不会猜测字段含义。

### 资金明细：`GET /api/v1/fund-details`

当前版本可以切换到“资金明细”页面并读取当前可见表格。`start_date` 和 `end_date` 已保留在 HTTP 契约中，但尚未通过 GUI 控件设置日期，因此目前不会自动改变同花顺页面的查询日期。

## 数据解析和校验逻辑

数字解析会处理以下常见情况：

- `1,234.50` 和中文千分位。
- `--`、`-`、空文本等占位符。
- 百分号和“万”“亿”等单位。
- 不能可靠转换的文本返回 `null`，不会伪造为 `0`。

资金数据会做基本勾稽检查。持仓数据会检查代码列，表格缺失时抛出明确错误。委托状态不会在文本不完整时自行补全。

## 为什么不使用固定坐标和截图 OCR

同花顺窗口可能发生移动、缩放、换肤或版本升级。固定坐标只能说明某个像素位置，不代表该位置仍然是同一个控件。Accessibility 能直接读取控件角色、标题、值和表格结构，因此更适合做长期维护的适配层。

截图和 OCR 可以作为未来的失败留证或兜底方案，但不应成为正常查询路径。

## 查询时的副作用边界

查询持仓、委托、成交和资金明细时，程序可能让同花顺切换到相应的查询标签页。这属于读取流程中的界面导航，不会提交订单，也不会改变账户资金。

默认运行时不会自动执行以下动作：

- 输入股票代码和价格。
- 点击买入或卖出。
- 提交订单（除非显式开启交易操作开关）。
- 撤单（除非显式开启交易操作开关）。
- 因读取超时而重试交易。

## 交易操作安全流程

买入、卖出和撤单都保持策略层与 GUI 细节隔离，并采用以下流程：

```text
订单意图
    -> 参数校验
    -> 订单预览
    -> 人工明确确认
    -> GUI 提交
    -> 重新读取委托 / 成交页面核对
```

### 单请求下单：`POST /api/v1/orders/execute`

策略层使用这个接口提交订单意图，不需要先创建预览再在 30 秒内手动提交。`preview` / `submit` 两阶段接口保留给人工排查和调试；它们的 30 秒有效期是旧预览凭证的安全窗口，并不是 GUI 操作本身需要 30 秒。

单请求管线在同一个 HTTP 请求中完成：

```text
显式 confirm=true
    -> 读取账户资金和持仓
    -> 输入代码、Tab 失焦并刷新一次五档
    -> 选择对手方第 N 档报价
    -> 按金额换算整手数量，或使用明确数量
    -> 写入价格和数量并回读校验
    -> 点击两层确认
    -> 重新读取委托表
    -> 返回受理状态、匹配委托与分段耗时
```

买入使用卖方档位，卖出使用买方档位：`price_level=1` 分别表示卖一/买一，`price_level=5` 分别表示卖五/买五。价格是限价，不会因为 GUI 的后续耗时突破选定档位成交；价格离开该档位时，订单可能留在委托表中。

`amount` 表示本次最多使用的金额。服务以选定档位价格向下取整到 100 股整手，因此实际金额由响应中 `preview.estimated_amount` 给出且不超过 `amount`。金额不足一手会拒绝，不会发出零股订单。需要精确数量时传 `quantity`，且必须是 100 的倍数；`amount` 与 `quantity` 必须二选一。

可选的 `max_price`（仅买入）和 `min_price`（仅卖出）是额外价格边界。选中档位超过边界时，服务在任何提交按钮被点击前拒绝请求。

响应中的 `timings_ms` 全部以毫秒计。查询和预览响应使用统一的 `timings_ms.total`；下单和撤单 Pipeline 还会返回下面列出的阶段字段。表中的耗时是当前 Mac、当前同花顺窗口和当前控件树规模下的实测参考范围，不是 SLA；冷启动、窗口切换和客户端刷新会造成抖动。

- `account_check`：账户、资金和持仓读取。
- `quote_refresh`：代码输入、失焦和五档读取。
- `form_fill`：价格和数量输入、回读验证及买入/卖出表单切换。
- `confirmation`：点击“确定买入/卖出”、处理第二层确认和业务错误弹窗。
- `order_refresh`：回读委托表并查找匹配订单。
- `total`：从请求开始到响应生成的总时长，包含板块切换及上述所有阶段。

Accessibility 控件树读取是当前延迟的主要来源。完整快照需要递归读取同花顺窗口的数千个控件，单次大约 1 秒；旧实现为价格、数量和按钮重复建立快照。当前买入管线会复用表单快照，并对单个文本框做实时属性读取，因此热路径实测约 14 秒，首次唤醒同花顺 Accessibility 服务时可能出现约 20 到 24 秒的冷启动抖动。冷启动抖动不能通过盲目缩短等待消除，否则可能在盘口或数量尚未刷新时提交订单。

如果请求在第一次“确定买入/卖出”之后超时，禁止直接重发该请求；先查询 `/api/v1/orders`，避免重复委托。

GUI 操作不具备天然幂等性，因此提交后如果出现超时，不能直接重复点击。必须先读取委托和成交结果，确认没有已提交订单后才能决定下一步。

买入或卖出提交还会处理同花顺的第二层确认框和业务错误弹窗：

```text
填写代码 / 价格 / 数量
    -> 逐字段回读校验
    -> 点击“确定买入”或“确定卖出”
    -> 读取当前焦点窗口
    -> 校验股票代码、价格、数量完全匹配
    -> 点击第二层“确认”
    -> 回读委托表核对
```

同花顺正常的第二层委托确认框也可能带有“警告”标题，因此不能仅凭标题判断失败。适配器会优先识别“证券交易未初始化”等明确业务错误；这类弹窗只点击一次“确认”关闭，并返回 `accepted=false`。正常确认框必须同时校验股票代码、数量和价格后才点击“确认”。

`accepted=true` 只表示 GUI 已完成确认点击且没有发现业务错误，不等于券商已经受理。最终是否形成委托必须以 `/api/v1/orders` 回读为准；如果服务重启、请求超时或客户端弹窗状态不明，禁止自动重试点击。

撤单也有独立的二次确认，不能把点击“撤买”或“撤卖”当作完成：

```text
按合同编号定位委托行
    -> 清除其他行的选中状态
    -> 只选中目标委托
    -> 点击“撤买”或“撤卖”
    -> 读取“撤单委托”确认框
    -> 必须确认窗口显示“撤销这 1 笔委托”
    -> 点击“确认”
    -> 回读委托表确认目标消失或状态已撤
```

同花顺允许多选委托。如果确认框显示“撤销这 2 笔委托”或其他数量，适配器会点击“取消”并返回失败，绝不会自动确认多笔撤单。当前版本撤单确认框不显示合同编号，因此单笔校验依赖于提交前按合同编号定位目标行，并强制确认窗口只包含 1 笔。

价格控件按以下顺序处理：

- 如果客户端暴露可写价格文本框，使用 `AXValue` 写入并回读。
- 如果价格文本框不存在，必须在买卖五档表中找到与请求价格完全一致的 `AXButton`，通过 `AXPress` 选择该档位。
- 如果两者都不存在（例如当前行情显示为 `--`），接口会拒绝提交；不会把价格写入代码、股东账号或数量控件。

## 安装

```bash
conda activate test
python -m pip install -e '.[dev]'
pytest
```

运行程序的终端或 Python 可执行文件需要获得 macOS 辅助功能权限：

**系统设置 > 隐私与安全性 > 辅助功能**。

## 启动

```bash
conda activate test
python -m thsgui_trader
```

服务默认监听 `127.0.0.1:8787`。

## 接口

```text
GET /health
GET /api/v1/connection
GET /api/v1/funds
GET /api/v1/positions
GET /api/v1/orders
GET /api/v1/trades
GET /api/v1/fund-details?start_date=2026-01-01&end_date=2026-01-31
GET /api/v1/order-book/603876
POST /api/v1/orders/preview
POST /api/v1/orders/submit
POST /api/v1/orders/execute
POST /api/v1/cancel/preview
POST /api/v1/cancel/submit
POST /api/v1/cancel/batch/preview
POST /api/v1/cancel/batch/submit
```

接口说明：

- `/health`：检查服务和同花顺连接状态。
- `/api/v1/connection`：查询同花顺进程、连接状态和账户模式。
- `/api/v1/funds`：查询总资产、总市值、盈亏和资金余额。
- `/api/v1/positions`：查询当前持仓。
- `/api/v1/orders`：查询委托记录。
- `/api/v1/trades`：查询成交记录。
- `/api/v1/fund-details`：查询资金明细。
- `/api/v1/order-book/{symbol}`：读取指定股票的买卖五档。
- `/api/v1/orders/preview`：校验并生成买入或卖出预览，不提交订单。
- `/api/v1/orders/submit`：在预览有效且明确确认后提交订单。
- `/api/v1/orders/execute`：在一个请求内刷新盘口、按档位定价、换算数量、提交订单并回读委托，适合策略层调用。
- `/api/v1/cancel/preview`：校验并生成指定委托的撤单预览。
- `/api/v1/cancel/submit`：在撤单预览有效且明确确认后，按 `order_id` 选中并撤销指定委托。
- `/api/v1/cancel/batch/preview`：按 `scope=buy|sell|all` 固定一组当前可撤委托，返回每个目标的 `order_id`。
- `/api/v1/cancel/batch/submit`：只选中预览中的委托行，校验确认框笔数与预览一致后执行撤买、撤卖或全撤。

订单预览示例：

```json
{
  "symbol": "603876",
  "side": "buy",
  "board": "A股",
  "price": 17.77,
  "quantity": 100,
  "order_type": "limit"
}
```

提交订单必须使用预览接口返回的 `preview_id`，并明确传递 `confirm: true`：

```json
{
  "preview_id": "预览接口返回的 ID",
  "confirm": true
}
```

策略下单示例。以下请求代表“最多买入 10,000 元 `000001`，使用卖二价格；若卖二高于 11.70 元则不下单”：

```bash
curl -X POST http://127.0.0.1:8787/api/v1/orders/execute -H 'Content-Type: application/json' -d '{"symbol":"000001","side":"buy","amount":10000,"price_level":2,"max_price":11.70,"confirm":true}'
```

精确卖出 100 股并使用买二价格：

```bash
curl -X POST http://127.0.0.1:8787/api/v1/orders/execute -H 'Content-Type: application/json' -d '{"symbol":"000001","side":"sell","quantity":100,"price_level":2,"min_price":11.50,"confirm":true}'
```

模拟盘卖出使用同一条管线，只需指定板块；卖出会先读取模拟盘持仓和可卖数量，再使用买方五档定价：

```bash
curl -X POST http://127.0.0.1:8787/api/v1/orders/execute -H 'Content-Type: application/json' -d '{"symbol":"000001","side":"sell","board":"模拟","quantity":100,"price_level":2,"min_price":11.50,"confirm":true}'
```

单笔撤单需要先按委托号生成预览，再提交确认：

```bash
curl -X POST http://127.0.0.1:8787/api/v1/cancel/preview -H 'Content-Type: application/json' -d '{"order_id":"委托编号","board":"A股"}'
curl -X POST http://127.0.0.1:8787/api/v1/cancel/submit -H 'Content-Type: application/json' -d '{"preview_id":"预览返回的 ID","confirm":true}'
```

批量撤买、撤卖和全撤使用同样的两阶段保护。预览响应中的 `orders` 是本次允许操作的精确集合；提交时如果客户端确认框显示的笔数与集合不一致，服务会点击取消并返回 `accepted=false`，不会误撤其他委托：

```bash
curl -X POST http://127.0.0.1:8787/api/v1/cancel/batch/preview -H 'Content-Type: application/json' -d '{"scope":"buy","board":"A股"}'
curl -X POST http://127.0.0.1:8787/api/v1/cancel/batch/submit -H 'Content-Type: application/json' -d '{"preview_id":"预览返回的 ID","confirm":true}'
```

## 耗时字段

所有真正执行 GUI 动作的响应都返回 `timings_ms`，单位是毫秒，`total` 是从网关开始到委托表回读结束的完整耗时，不是预览有效期：

| 层级 | 接口/字段 | 实测参考范围 | 含义 |
| --- | --- | ---: | --- |
| 子功能 | `/health`、`/connection` | 0.9--1.2 秒 | 连接状态和进程控件树读取 |
| 子功能 | `/funds` | 0.9--1.2 秒 | 资金页面读取和解析 |
| 子功能 | `/positions` | 2.3--2.6 秒 | 切持仓页、读取和解析持仓 |
| 子功能 | `/orders` | 2.3--2.6 秒 | 切委托页、读取和解析委托 |
| 子功能 | `/trades` | 2.3--2.6 秒 | 切成交页、读取和解析成交 |
| 子功能 | `/fund-details` | 2.3--2.6 秒 | 切资金明细页、读取和解析 |
| 子功能 | `/order-book/{symbol}` | 7.5--8.5 秒 | 切板块、输入代码、刷新和读取买卖五档 |
| 子功能 | `/orders/preview` | 通常 10--15 秒 | 资金/持仓读取、盘口读取和生成预览，不点击下单 |
| Pipeline | `/orders/submit` | 通常 8--14 秒 | 使用预览填写表单、二次确认并回读委托；策略层优先使用 `/orders/execute` |
| 子功能 | `/cancel/preview` | 3.5--6 秒 | 切板块、读取委托并按 `order_id` 固定目标 |
| 子功能 | `/cancel/batch/preview` | 5--6 秒 | 切板块、读取委托并固定目标集合 |
| Pipeline | `/orders/execute` 买入 | 14--20 秒 | `account_check`、`quote_refresh`、`form_fill`、确认和委托回读 |
| Pipeline | `/orders/execute` 卖出 | 17--20 秒 | 额外包含持仓读取；卖出使用买方第 N 档 |
| Pipeline 子阶段 | `account_check` | 2.8--5.3 秒 | 选择板块、读取资金；卖出还包含持仓 |
| Pipeline 子阶段 | `quote_refresh` | 3.4--5.3 秒 | 输入代码、失焦刷新和读取五档 |
| Pipeline 子阶段 | `form_fill` | 3.7--3.9 秒 | 写入价格/数量、回读校验、点击第一层买入/卖出 |
| Pipeline 子阶段 | `confirmation` | 1.8--1.9 秒 | 校验并点击二次确认及处理业务弹窗 |
| Pipeline 子阶段 | `order_refresh` | 2.2--2.6 秒 | 切到委托页并查找匹配委托 |
| Pipeline | `/cancel/submit` 撤买/撤卖 | 约 5--11 秒 | `selection`、单笔确认和委托回读；按委托号定位 |
| Pipeline 子阶段 | `selection` | 4.7--5.0 秒 | 切板块、切委托页、按委托号选行并点击撤单 |
| Pipeline 子阶段 | `confirmation` | 0.3--2.0 秒 | 校验笔数并点击确认；数量不一致会取消 |
| Pipeline 子阶段 | `order_refresh` | 2.3--2.6 秒 | 回读目标委托状态 |
| Pipeline | `/cancel/batch/submit` 撤买/撤卖/全撤 | 约 9--12 秒 | 选择全部目标行、确认精确笔数并回读 |

实测热路径的主要耗时来自 Accessibility 控件树快照；当前环境买入约 14 秒，冷启动可能约 20--24 秒。撤单通常少一次表单输入，但仍受同花顺页面切换和控件树刷新影响。服务不会为了压缩耗时而省略字段回读、确认框校验或盲目重试；发生超时应先调用 `/api/v1/orders` 对账。

这里的“子功能”是可以独立调用的读取、刷新、选行或确认步骤；“Pipeline”是策略层真正调用的一次完整买入、卖出或撤单请求。子功能耗时不能简单相加，因为部分快照和页面状态会在同一请求内复用；策略预估应优先使用对应 Pipeline 的 `timings_ms.total`。

## 当前限制

- 委托、成交和资金明细通过同花顺当前可见的语义控件读取。
- 当前同花顺版本的交易区可能不暴露可写价格框；非交易时段五档显示 `--` 时，不能通过本适配器提交任意价格订单。
- 委托表无记录时只能返回空数组；撤单必须先通过委托查询得到委托编号。
- 资金明细的日期控件尚未适配当前客户端版本，日期参数暂作为接口契约保留。
- 读取失败时返回错误，不会自动重试下单或执行其他有资金影响的操作。
- 交易操作默认关闭，即使调用提交接口也不会点击交易按钮。
- 模拟账户测试需要显式设置 `THSGUI_ALLOW_ORDER_ACTIONS=true`。
- A 股账户还需要额外设置 `THSGUI_ALLOW_LIVE_ORDERS=true`，否则只允许查询和生成预览。

## 交易操作的安全开关

默认配置只允许读取和预览：

```bash
THSGUI_ALLOW_ORDER_ACTIONS=false
THSGUI_ALLOW_LIVE_ORDERS=false
```

如果要在模拟账户中测试提交和撤单，必须明确开启：

```bash
export THSGUI_ALLOW_ORDER_ACTIONS=true
python -m thsgui_trader
```

`THSGUI_ALLOW_LIVE_ORDERS` 默认关闭。即使当前不在交易日，也不能把非交易日等同于“没有风险”；委托可能进入挂单、预约或券商队列，因此 A 股账户需要额外的显式配置。

## A 股和模拟盘测试流程

A 股板块是读取五档、填写委托、提交委托和撤单的实盘适配路径；模拟板块用于有持仓时验证卖出和撤卖。网关不会依赖当前窗口是否已经停留在交易页面：每次读取盘口或执行订单操作前，都会通过 Accessibility 查找并按下请求中的板块按钮，然后再写入股票代码。

典型验证顺序如下：

```text
A 股/模拟按钮
  -> 键盘事件写入股票代码并 Tab 失焦刷新
  -> 读取买卖五档
  -> 同一请求内按对手方档位确定价格和数量
  -> 点击“确定买入/确定卖出”一次
  -> 校验二次确认窗口
  -> 回读委托表
  -> 生成撤单预览并点击撤买/撤卖一次
  -> 回读委托状态
```

如果客户端没有暴露可写价格框，价格只能来自买卖五档中的可按控件。非交易时段五档常显示 `--`，此时接口仍可生成带告警的预览，但提交接口会返回 `trade form price control is not exposed`，不会把价格误写入股东账号或数量框，也不会点击确认按钮。对于暴露可写价格框的版本，适配器只接受价格文本框本身的回读值，不能用盘口上同价按钮替代；因此客户端回填了不同价格时会在点击确认前拒绝。

代码刷新前会把数量框临时设为 `0`，避免旧数量和价格使提交式键盘事件触发交易校验；正式提交时再写入请求数量。读取盘口因此不会点击“确定买入/卖出”，也不会产生委托。

启动委托测试服务需要显式打开两个开关：

```bash
conda activate test
PYTHONPATH=src \
THSGUI_ALLOW_ORDER_ACTIONS=true \
THSGUI_ALLOW_LIVE_ORDERS=true \
python -m uvicorn thsgui_trader.main:app --host 127.0.0.1 --port 8787
```

这两个开关只表示“允许适配器执行 GUI 委托动作”，不代表券商一定不会接收委托。测试前仍需确认账户、股票、价格和数量，提交后以委托表回读为准；遇到超时不得自动重试。
