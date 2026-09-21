"""Nuitka 배포 바이너리 진입점."""

from __future__ import annotations

import os

import uvicorn

from app.main import app


def main() -> None:
    uvicorn.run(
        app,
        host=os.getenv("UVICORN_HOST", "0.0.0.0"),
        port=int(os.getenv("UVICORN_PORT", "38085")),
        log_level=os.getenv("LOG_LEVEL", "INFO").lower(),
    )


if __name__ == "__main__":
    main()
