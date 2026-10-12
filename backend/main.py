#!/usr/bin/env python3
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from meizang.server import serve


def main():
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="媒藏 media manager web service")
    parser.add_argument("--host", default=os.environ.get("MEIZANG_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MEIZANG_PORT", "8787")))
    parser.add_argument("--socket", default=os.environ.get("MEIZANG_SOCKET", ""))
    parser.add_argument("--prefix", default=os.environ.get("MEIZANG_PREFIX", "/app/meizang"))
    parser.add_argument("--database", default=os.environ.get("MEIZANG_DATABASE", str(project_root / "data" / "library.db")))
    parser.add_argument("--static", default=os.environ.get("MEIZANG_STATIC", str(project_root / "frontend")))
    parser.add_argument("--token", default=os.environ.get("MEIZANG_TOKEN", ""),
                        help="API 访问令牌；设置后 /api/*（除 /api/health 外）需要 Authorization: Bearer 认证")
    arguments = parser.parse_args()
    serve(arguments.database, arguments.static, arguments.host, arguments.port, arguments.socket, arguments.prefix,
          arguments.token)


if __name__ == "__main__":
    main()
