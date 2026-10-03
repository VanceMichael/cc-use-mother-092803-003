"""预售资金监管的轻量本地调用入口。

用法::

    echo '{"actor":"admin","action":"create_project","request_id":"r1",
           "payload":{"project_id":"P1","name":"春风里"}}' \
        | python3 -m app.api --db escrow.db

数据库路径也可用环境变量 ESCROW_DB 指定，默认 :memory:（进程退出即清空，仅用于演示）。
"""
import json
import os
import sys
from datetime import datetime, timezone

from .contracts import Request
from .service import HousingEscrowService
from .store import Store


def _resolve_db(argv: list[str]) -> str:
    for i, arg in enumerate(argv):
        if arg == "--db" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--db="):
            return arg.split("=", 1)[1]
    return os.environ.get("ESCROW_DB", ":memory:")


def main() -> int:
    db_path = _resolve_db(sys.argv[1:])
    service = HousingEscrowService(Store(db_path))
    raw = sys.stdin.read().strip()
    if not raw:
        sys.stderr.write("用法: 请求 JSON 通过 stdin 传入，--db 指定 SQLite 文件\n")
        return 2
    item = json.loads(raw)
    request = Request(
        str(item.get("actor", "")),
        str(item.get("action", "")),
        dict(item.get("payload", {})),
        str(item.get("request_id", "")),
        datetime.now(timezone.utc),
    )
    result = service.handle(request)
    print(json.dumps({
        "accepted": result.accepted,
        "state": result.state,
        "message": result.message,
        "data": result.data,
    }, ensure_ascii=False))
    return 0 if result.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
