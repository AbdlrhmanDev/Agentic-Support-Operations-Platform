"""Run the API: `uv run python -m app`. Works on Windows, unlike a bare `uvicorn` call."""

import os

import uvicorn

from app.eventloop import run


async def serve() -> None:
    config = uvicorn.Config(
        "app.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
    await uvicorn.Server(config).serve()


if __name__ == "__main__":
    run(serve())
