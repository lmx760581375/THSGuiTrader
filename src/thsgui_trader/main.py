from .api import app
from .config import Settings


def run() -> None:
    import uvicorn

    settings = Settings.from_env()
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    run()
