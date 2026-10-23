"""单独起靶机，方便用 curl / Postman 手工摸接口。

    python -m apilab.server_main --port 8000 --verbose
"""

from __future__ import annotations

import argparse

from apilab.fake_server import ApiLabServer


def main() -> None:
    parser = argparse.ArgumentParser(description="启动本地 REST 靶机")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--seed-books", type=int, default=25)
    parser.add_argument("--verbose", action="store_true", help="打印每条请求日志")
    args = parser.parse_args()

    server = ApiLabServer(port=args.port, verbose=args.verbose, seed_books=args.seed_books)
    print(f"靶机已启动：{server.base_url}")
    print("账号：alice/alice-pw（user）  admin/admin-pw（admin）")
    print("Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
