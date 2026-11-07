"""一条命令启动：python -m app"""
from __future__ import annotations

from app.api.server import serve

if __name__ == "__main__":
    serve()
