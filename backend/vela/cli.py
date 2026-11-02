"""Process roles used by the two application containers."""
from __future__ import annotations

import argparse
import asyncio
import os

from .config import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vela")
    commands = parser.add_subparsers(dest="command", required=True)
    api = commands.add_parser("api")
    api.add_argument("--host", default="0.0.0.0")
    api.add_argument("--port", default=8080, type=int)
    commands.add_parser("worker")
    health = commands.add_parser("health")
    health.add_argument("role", choices=["worker"])
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    if args.command == "api":
        import uvicorn
        uvicorn.run("vela.api:create_app", factory=True, host=args.host, port=args.port,
                    proxy_headers=True, forwarded_allow_ips=os.environ.get("VELA_TRUSTED_PROXIES", "127.0.0.1"))
        return 0
    from .worker import Worker, worker_healthy
    if args.command == "health":
        return 0 if worker_healthy(settings) else 1
    asyncio.run(Worker(settings).run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
