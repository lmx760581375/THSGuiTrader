from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    bundle_id: str = "cn.com.10jqka.macstock"
    host: str = "127.0.0.1"
    port: int = 8787
    request_timeout_seconds: float = 8.0
    max_tree_nodes: int = 3000
    allow_order_actions: bool = False
    allow_live_orders: bool = False
    preview_ttl_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            bundle_id=os.getenv("THSGUI_BUNDLE_ID", cls.bundle_id),
            host=os.getenv("THSGUI_HOST", cls.host),
            port=int(os.getenv("THSGUI_PORT", str(cls.port))),
            request_timeout_seconds=float(
                os.getenv("THSGUI_REQUEST_TIMEOUT", str(cls.request_timeout_seconds))
            ),
            max_tree_nodes=int(os.getenv("THSGUI_MAX_TREE_NODES", str(cls.max_tree_nodes))),
            allow_order_actions=os.getenv("THSGUI_ALLOW_ORDER_ACTIONS", "false").lower() == "true",
            allow_live_orders=os.getenv("THSGUI_ALLOW_LIVE_ORDERS", "false").lower() == "true",
            preview_ttl_seconds=float(os.getenv("THSGUI_PREVIEW_TTL", str(cls.preview_ttl_seconds))),
        )
