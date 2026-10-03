"""预售资金监管后端 HTTP 入口（标准库实现，无第三方依赖）。

启动：
    python3 -m app.api --db escrow.db --host 127.0.0.1 --port 8080

请求：
    POST /            {"request_id": "...", "action": "...", "payload": {...}}
    Authorization: Bearer <token>
    GET  /health      健康检查

首次启动会自动创建内置监管账号 regulator，并把其 token 打印到标准输出（仅一次）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .contracts import Request
from .service import HousingEscrowService, BOOTSTRAP_REGULATOR, bootstrap
from .store import Store


def authenticate(store: Store, header: str | None) -> str | None:
    if not header or not header.startswith("Bearer "):
        return None
    token = header[len("Bearer "):].strip()
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    row = store.conn.execute(
        "SELECT actor_id FROM api_tokens WHERE token = ? AND revoked = 0", (digest,)).fetchone()
    return row["actor_id"] if row else None


def build_handler(store: Store) -> type[BaseHTTPRequestHandler]:
    service = HousingEscrowService(store)

    class Handler(BaseHTTPRequestHandler):
        server_version = "PresaleEscrow/1.0"

        def _send(self, code: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
            return

        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/") in ("", "/health"):
                self._send(200, {"ok": True, "service": "presale-escrow"})
            else:
                self._send(404, {"accepted": False, "message": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.rstrip("/") not in ("", "/", "/rpc"):
                self._send(404, {"accepted": False, "message": "not found"})
                return
            actor_id = authenticate(store, self.headers.get("Authorization"))
            if actor_id is None:
                self._send(401, {"accepted": False, "state": "unauthorized",
                                 "message": "缺少或无效的 Bearer token"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except (ValueError, UnicodeDecodeError):
                self._send(400, {"accepted": False, "state": "invalid", "message": "请求体不是合法 JSON"})
                return
            action = str(body.get("action", ""))
            payload = body.get("payload", {})
            request_id = str(body.get("request_id") or uuid.uuid4().hex)
            if not isinstance(payload, dict):
                self._send(400, {"accepted": False, "state": "invalid", "message": "payload 必须是对象"})
                return
            result = service.handle(Request(actor_id, action, payload, request_id))
            code = 200 if result.accepted else (
                403 if result.state == "forbidden" else
                404 if result.state == "not_found" else
                409 if result.state == "conflict" else 422)
            self._send(code, {"accepted": result.accepted, "state": result.state,
                              "message": result.message, "data": result.data,
                              "request_id": request_id})

    return Handler


def serve(db_path: str, host: str, port: int) -> ThreadingHTTPServer:
    store = Store(db_path)
    token = bootstrap(store)
    if token:
        print(f"[bootstrap] 已创建内置监管账号 {BOOTSTRAP_REGULATOR}，token（仅显示一次）：{token}",
              flush=True)
    server = ThreadingHTTPServer((host, port), build_handler(store))
    print(f"[serve] 预售资金监管后端监听 http://{host}:{port} （数据库 {db_path}）", flush=True)
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="预售资金监管后端")
    parser.add_argument("--db", default="escrow.db", help="SQLite 数据库文件")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = serve(args.db, args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
