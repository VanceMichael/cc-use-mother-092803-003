"""幂等性：请求重放、银行回调重送、指令重复结算。"""
import unittest

from tests.world import (call, fresh, pending_instructions, settle_in,
                         settle_out, sign_default)


class IdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.svc, _ = fresh()
        sign_default(self.svc)

    def test_request_replay_returns_same_result(self):
        req_payload = {"callback_id": "cb-1", "direction": "in",
                       "project_id": "p1", "contract_id": "c1",
                       "seq": 1, "amount": 30_000, "bank_ref": "REF-1"}
        first = call(self.svc, "bank1", "bank_callback", req_payload, rid="rid-1")
        second = call(self.svc, "bank1", "bank_callback", req_payload, rid="rid-1")
        self.assertTrue(first.accepted)
        self.assertEqual(first.data, second.data)
        r = call(self.svc, "reg", "get_account", {"project_id": "p1"})
        self.assertEqual(r.data["account"]["balance_yuan"], "30000.00")  # 只入一次账

    def test_bank_callback_resend_does_not_double_settle(self):
        settle_in(self.svc, "bank1", "c1", 1, 30_000, "cb-dep")
        settle_in(self.svc, "bank1", "c1", 2, 970_000, "cb-rest")
        call(self.svc, "reg", "record_event",
             {"contract_id": "c1", "event_type": "structure_top"})
        instr = pending_instructions(self.svc)[0]
        cb1 = settle_out(self.svc, "bank1", instr["id"], "cb-out-1")
        cb2 = settle_out(self.svc, "bank1", instr["id"], "cb-out-1")  # 同 callback_id
        cb3 = settle_out(self.svc, "bank1", instr["id"], "cb-out-2")  # 换编号重送
        self.assertTrue(cb1.accepted)
        self.assertTrue(cb2.data.get("deduplicated"))
        self.assertTrue(cb3.data.get("deduplicated"))
        r = call(self.svc, "reg", "get_account", {"project_id": "p1"})
        # 100 万只划出一次 60 万
        self.assertEqual(r.data["account"]["balance_yuan"], "400000.00")

    def test_inflow_with_new_callback_id_for_paid_item_is_deduped(self):
        settle_in(self.svc, "bank1", "c1", 1, 30_000, "cb-a")
        again = settle_in(self.svc, "bank1", "c1", 1, 30_000, "cb-b")
        self.assertTrue(again.accepted)
        self.assertTrue(again.data.get("deduplicated"))
        r = call(self.svc, "reg", "get_account", {"project_id": "p1"})
        self.assertEqual(r.data["account"]["balance_yuan"], "30000.00")

    def test_terminal_event_replay_creates_one_refund(self):
        settle_in(self.svc, "bank1", "c1", 1, 30_000, "cb-dep")
        payload = {"contract_id": "c1", "event_type": "overdue"}
        first = call(self.svc, "reg", "record_event", payload, rid="evt-rid-1")
        second = call(self.svc, "reg", "record_event", payload, rid="evt-rid-1")
        self.assertTrue(first.accepted)
        self.assertEqual(first.data, second.data)
        instrs = pending_instructions(self.svc)
        self.assertEqual(len(instrs), 1)


if __name__ == "__main__":
    unittest.main()
