"""服务入口的基础契约：未知动作、缺字段、请求级幂等。"""
import unittest

from app.contracts import Request
from app.service import HousingEscrowService
from app.store import Store


class ServiceEntryTest(unittest.TestCase):
    def setUp(self):
        store = Store(":memory:")
        with store.tx() as c:
            c.execute("INSERT INTO actors (actor_id, role, display_name, scope_id)"
                      " VALUES ('reg', 'regulator', '监管员', NULL)")
        self.svc = HousingEscrowService(store)

    def test_unknown_action(self):
        r = self.svc.handle(Request("reg", "fly_to_moon", {}, "r-0"))
        self.assertFalse(r.accepted)

    def test_unknown_actor(self):
        r = self.svc.handle(Request("nobody", "get_project", {"project_id": "x"}, "r-1"))
        self.assertFalse(r.accepted)
        self.assertEqual(r.state, "forbidden")

    def test_request_level_idempotency(self):
        payload = {"id": "d1", "name": "甲地产"}
        first = self.svc.handle(Request("reg", "register_developer", payload, "r-2"))
        second = self.svc.handle(Request("reg", "register_developer", payload, "r-2"))
        self.assertTrue(first.accepted)
        self.assertEqual(first.data, second.data)
        # 同一 request_id 即使换动作/参数，也只返回首次结果
        other = self.svc.handle(Request("reg", "register_bank", {"id": "b"}, "r-2"))
        self.assertEqual(other.data, first.data)

    def test_missing_identity_rejected(self):
        with self.assertRaises(ValueError):
            self.svc.handle(Request("", "get_project", {}, "r-3"))


if __name__ == "__main__":
    unittest.main()
