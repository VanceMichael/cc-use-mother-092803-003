"""HTTP API 端到端：Bearer 鉴权、动作分发、错误码、重放。"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from app.api import build_handler, bootstrap
from app.contracts import Request
from app.service import HousingEscrowService
from app.store import Store
from http.server import ThreadingHTTPServer


class HttpApiTest(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = Store(self.db)
        self.reg_token = bootstrap(self.store)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(self.store))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        os.unlink(self.db)

    def _rpc(self, token, body, expect_status=None):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/", data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req) as resp:
                status = resp.status
                parsed = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            status = e.code
            parsed = json.loads(e.read().decode("utf-8"))
        if expect_status is not None:
            self.assertEqual(status, expect_status, parsed)
        return parsed

    def _setup_project(self):
        svc = HousingEscrowService(self.store)
        svc.handle(Request("regulator", "register_developer",
                           {"id": "d1", "name": "甲地产"}, "setup-1"))
        svc.handle(Request("regulator", "register_bank",
                           {"id": "b1", "name": "甲行"}, "setup-2"))
        svc.handle(Request("regulator", "create_project",
                           {"id": "p1", "developer_id": "d1", "bank_id": "b1",
                            "name": "院"}, "setup-3"))
        svc.handle(Request("regulator", "register_actor",
                           {"id": "dev1", "role": "developer", "scope_id": "d1"},
                           "setup-4"))
        issued = svc.handle(Request("regulator", "issue_token",
                                   {"actor_id": "dev1"}, "setup-5"))
        return issued.data["token"]

    def test_health(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health") as r:
            self.assertEqual(json.loads(r.read())["ok"], True)

    def test_missing_token_401(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 401)

    def test_bad_token_401(self):
        parsed = self._rpc("not-a-real-token",
                           {"action": "get_project", "payload": {"project_id": "p1"}},
                           expect_status=401)
        self.assertFalse(parsed["accepted"])

    def test_rbac_enforced_over_http(self):
        dev_token = self._setup_project()
        # dev1 只能处理 d1 的项目；访问不存在的项目返回 not_found/forbidden 链路可用
        ok = self._rpc(dev_token, {"request_id": "http-1", "action": "get_project",
                                   "payload": {"project_id": "p1"}}, expect_status=200)
        self.assertTrue(ok["accepted"])

    def test_forbidden_maps_403(self):
        dev_token = self._setup_project()
        # 项目公司无权登记机构
        parsed = self._rpc(dev_token, {"request_id": "http-2",
                                       "action": "register_bank",
                                       "payload": {"id": "b9", "name": "野行"}},
                           expect_status=403)
        self.assertEqual(parsed["state"], "forbidden")

    def test_conflict_maps_409_and_replay_is_idempotent(self):
        svc = HousingEscrowService(self.store)
        svc.handle(Request("regulator", "register_developer",
                           {"id": "d1", "name": "甲"}, "h-1"))
        svc.handle(Request("regulator", "register_bank",
                           {"id": "b1", "name": "行"}, "h-2"))
        body = {"request_id": "h-3", "action": "create_project",
                "payload": {"id": "p1", "developer_id": "d1",
                            "bank_id": "b1", "name": "院"}}
        first = self._rpc(self.reg_token, body, expect_status=200)
        second = self._rpc(self.reg_token, body, expect_status=200)
        self.assertEqual(first, second)
        # 再用新 request_id 建同 id 项目 -> 唯一约束冲突 -> 409
        body2 = dict(body, request_id="h-4")
        parsed = self._rpc(self.reg_token, body2, expect_status=409)
        self.assertFalse(parsed["accepted"])

    def test_revoked_token_rejected(self):
        svc = HousingEscrowService(self.store)
        svc.handle(Request("regulator", "register_developer",
                           {"id": "d1", "name": "甲"}, "rv-1"))
        issued = svc.handle(Request("regulator", "register_actor",
                                   {"id": "dev1", "role": "developer",
                                    "scope_id": "d1"}, "rv-2"))
        token = svc.handle(Request("regulator", "issue_token",
                                   {"actor_id": "dev1"}, "rv-3")).data["token"]
        # 项目不存在，但认证已通过（404 业务错误，不是 401）
        self._rpc(token, {"action": "get_project",
                          "payload": {"project_id": "p1"}}, expect_status=404)
        svc.handle(Request("regulator", "revoke_token", {"token": token}, "rv-4"))
        self._rpc(token, {"action": "get_project",
                          "payload": {"project_id": "p1"}}, expect_status=401)


if __name__ == "__main__":
    unittest.main()
