"""预售资金监管的本地命令行入口（供脚本/运维调用，不走 HTTP token）。

    echo '{"actor":"regulator","action":"create_project",
           "payload":{"id":"p1",...},"request_id":"r1"}' | python3 -m app.cli --db escrow.db
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from .contracts import Request
from .service import HousingEscrowService, bootstrap
from .store import Store


def run(db_path: str, item: dict) -> int:
    store = Store(db_path)
    try:
        token = bootstrap(store)
        if token:
            import sys
            print(f"[bootstrap] 已创建内置监管账号 regulator，API token（仅显示一次）：{token}",
                  file=sys.stderr, flush=True)
        service = HousingEscrowService(store)
        request = Request(
            str(item.get("actor", "")),
            str(item.get("action", "")),
            dict(item.get("payload", {})),
            str(item.get("request_id", "")),
            datetime.now(timezone.utc),
        )
        result = service.handle(request)
        print(json.dumps({"accepted": result.accepted, "state": result.state,
                          "message": result.message, "data": result.data}, ensure_ascii=False))
        return 0 if result.accepted else 1
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="预售资金监管本地调用入口")
    parser.add_argument("--db", default="escrow.db", help="SQLite 数据库文件")
    args = parser.parse_args()
    import sys
    raw = sys.stdin.read().strip()
    if not raw:
        return 2
    return run(args.db, json.loads(raw))


if __name__ == "__main__":
    raise SystemExit(main())
