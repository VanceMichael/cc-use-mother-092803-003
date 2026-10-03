"""业务规则：3% 定金上限、节点顺序、各事件的确定资金结果。"""
import unittest

from tests.world import (balance, call, fresh, pending_instructions,
                         settle_in, settle_out, sign_default)


class DepositCapTest(unittest.TestCase):
    def setUp(self):
        self.svc, _ = fresh()

    def test_deposit_at_cap_accepted(self):
        r = sign_default(self.svc, deposit=30_000)  # 总价 100 万的 3%
        self.assertTrue(r.accepted, r.message)
        self.assertEqual(r.data["deposit_cap_yuan"], "30000.00")

    def test_deposit_over_cap_rejected(self):
        r = sign_default(self.svc, "c-bad", deposit=30_000.01)
        self.assertFalse(r.accepted)
        self.assertEqual(r.state, "invalid")
        self.assertIn("3%", r.message)

    def test_zero_deposit_rejected(self):
        r = sign_default(self.svc, "c-zero", deposit=0)
        self.assertFalse(r.accepted)


class NodeOrderTest(unittest.TestCase):
    def setUp(self):
        self.svc, _ = fresh()
        sign_default(self.svc)

    def test_top_out_before_deposit_rejected(self):
        # 合同尚在 draft（定金未到账），不能封顶
        r = call(self.svc, "reg", "record_event",
                 {"contract_id": "c1", "event_type": "structure_top"})
        self.assertFalse(r.accepted)
        self.assertEqual(r.state, "conflict")

    def test_acceptance_before_top_out_rejected(self):
        settle_in(self.svc, "bank1", "c1", 1, 30_000, "cb-d")
        r = call(self.svc, "reg", "record_event",
                 {"contract_id": "c1", "event_type": "acceptance"})
        self.assertFalse(r.accepted)
        self.assertIn("封顶", r.message)

    def test_event_after_terminal_rejected(self):
        settle_in(self.svc, "bank1", "c1", 1, 30_000, "cb-d")
        call(self.svc, "reg", "record_event",
             {"contract_id": "c1", "event_type": "overdue"})
        r = call(self.svc, "reg", "record_event",
                 {"contract_id": "c1", "event_type": "structure_top"})
        self.assertFalse(r.accepted)


class HappyPathTest(unittest.TestCase):
    def setUp(self):
        self.svc, _ = fresh()
        sign_default(self.svc)
        settle_in(self.svc, "bank1", "c1", 1, 30_000, "cb-deposit")
        settle_in(self.svc, "bank1", "c1", 2, 970_000, "cb-rest")

    def test_funds_enter_escrow_only(self):
        self.assertEqual(balance(self.svc), "1000000.00")
        r = call(self.svc, "reg", "get_contract", {"contract_id": "c1"})
        self.assertEqual(r.data["state"], "active")

    def test_top_out_releases_60_percent(self):
        r = call(self.svc, "reg", "record_event",
                 {"contract_id": "c1", "event_type": "structure_top"})
        self.assertTrue(r.accepted, r.message)
        self.assertEqual(r.data["contract_status"], "topped_out")
        instrs = pending_instructions(self.svc)
        self.assertEqual(len(instrs), 1)
        self.assertEqual(instrs[0]["flow_type"], "out_developer")
        self.assertEqual(instrs[0]["amount_yuan"], "600000.00")
        # 指令未结算前，监管账户一分不动，杜绝提前放款
        self.assertEqual(balance(self.svc), "1000000.00")

    def test_full_lifecycle_completes(self):
        call(self.svc, "reg", "record_event",
             {"contract_id": "c1", "event_type": "structure_top"})
        top_instr = pending_instructions(self.svc)[0]["id"]
        settle_out(self.svc, "bank1", top_instr, "cb-top")
        self.assertEqual(balance(self.svc), "400000.00")

        call(self.svc, "reg", "record_event",
                 {"contract_id": "c1", "event_type": "acceptance"})
        acc_instr = pending_instructions(self.svc)[0]["id"]
        self.assertEqual(
            [i["amount_yuan"] for i in pending_instructions(self.svc)], ["400000.00"])
        settle_out(self.svc, "bank1", acc_instr, "cb-acc")
        self.assertEqual(balance(self.svc), "0.00")
        r = call(self.svc, "reg", "get_contract", {"contract_id": "c1"})
        self.assertEqual(r.data["state"], "completed")


class ReleaseProRataTest(unittest.TestCase):
    def test_release_scales_with_amount_paid(self):
        """封顶时购房人只缴了定金 3 万：解冻只能按已缴金额的 60% = 1.8 万。"""
        svc, _ = fresh()
        sign_default(svc)
        settle_in(svc, "bank1", "c1", 1, 30_000, "cb-d")
        call(svc, "reg", "record_event",
             {"contract_id": "c1", "event_type": "structure_top"})
        instrs = pending_instructions(svc)
        self.assertEqual(instrs[0]["amount_yuan"], "18000.00")


class OverdueTest(unittest.TestCase):
    def test_overdue_refunds_retained_and_cancels_pending(self):
        svc, _ = fresh()
        sign_default(svc)
        settle_in(svc, "bank1", "c1", 1, 30_000, "cb-d")
        settle_in(svc, "bank1", "c1", 2, 970_000, "cb-r")
        # 封顶并拨付 60 万后逾期
        call(svc, "reg", "record_event",
             {"contract_id": "c1", "event_type": "structure_top"})
        settle_out(svc, "bank1", pending_instructions(svc)[0]["id"], "cb-top")
        r = call(svc, "reg", "record_event",
                 {"contract_id": "c1", "event_type": "overdue"})
        self.assertTrue(r.accepted, r.message)
        self.assertEqual(r.data["contract_status"], "overdue_closed")
        instrs = pending_instructions(svc)
        # 只剩退还留存 40 万这一条指令
        self.assertEqual(len(instrs), 1)
        self.assertEqual(instrs[0]["flow_type"], "out_buyer")
        self.assertEqual(instrs[0]["amount_yuan"], "400000.00")
        settle_out(svc, "bank1", instrs[0]["id"], "cb-ref")
        self.assertEqual(balance(svc), "0.00")


class DeveloperBreachTest(unittest.TestCase):
    def test_breach_refunds_retained_plus_penalty(self):
        svc, _ = fresh()
        sign_default(svc)
        settle_in(svc, "bank1", "c1", 1, 30_000, "cb-d")
        settle_in(svc, "bank1", "c1", 2, 970_000, "cb-r")
        call(svc, "reg", "record_event",
             {"contract_id": "c1", "event_type": "structure_top"})
        settle_out(svc, "bank1", pending_instructions(svc)[0]["id"], "cb-top")
        r = call(svc, "reg", "record_event", {
            "contract_id": "c1", "event_type": "developer_breach",
            "detail": {"penalty": 50_000}})
        self.assertTrue(r.accepted, r.message)
        instrs = pending_instructions(svc)
        flows = {i["flow_type"]: i["amount_yuan"] for i in instrs}
        self.assertEqual(flows["out_buyer"], "400000.00")       # 监管留存退还
        self.assertEqual(flows["penalty_developer"], "50000.00")  # 项目公司自付
        # 违约金结算不扣监管余额
        pen = next(i["id"] for i in instrs if i["flow_type"] == "penalty_developer")
        settle_out(svc, "bank1", pen, "cb-pen")
        self.assertEqual(balance(svc), "400000.00")
        ref = next(i["id"] for i in instrs if i["flow_type"] == "out_buyer")
        settle_out(svc, "bank1", ref, "cb-ref")
        self.assertEqual(balance(svc), "0.00")


class BuyerFaultTest(unittest.TestCase):
    def test_buyer_breach_before_any_release_forfeits_deposit(self):
        svc, _ = fresh()
        sign_default(svc)
        settle_in(svc, "bank1", "c1", 1, 30_000, "cb-d")
        settle_in(svc, "bank1", "c1", 2, 970_000, "cb-r")
        r = call(svc, "reg", "record_event",
                 {"contract_id": "c1", "event_type": "buyer_breach"})
        self.assertTrue(r.accepted, r.message)
        instrs = pending_instructions(svc)
        flows = {i["flow_type"]: i["amount_yuan"] for i in instrs}
        self.assertEqual(flows["penalty_buyer"], "30000.00")   # 定金归项目公司
        self.assertEqual(flows["out_buyer"], "970000.00")      # 尾款退还

    def test_buyer_breach_after_release_forfeits_from_retained_only(self):
        svc, _ = fresh()
        sign_default(svc)
        settle_in(svc, "bank1", "c1", 1, 30_000, "cb-d")
        settle_in(svc, "bank1", "c1", 2, 970_000, "cb-r")
        call(svc, "reg", "record_event",
             {"contract_id": "c1", "event_type": "structure_top"})
        settle_out(svc, "bank1", pending_instructions(svc)[0]["id"], "cb-top")
        # 监管留存 40 万；购房人违约，定金罚没不超过留存
        call(svc, "reg", "record_event",
             {"contract_id": "c1", "event_type": "cancel_buyer"})
        instrs = pending_instructions(svc)
        flows = {i["flow_type"]: i["amount_yuan"] for i in instrs}
        self.assertEqual(flows["penalty_buyer"], "30000.00")
        self.assertEqual(flows["out_buyer"], "370000.00")

    def test_acceptance_then_buyer_cancel_rejected(self):
        svc, _ = fresh()
        sign_default(svc)
        settle_in(svc, "bank1", "c1", 1, 30_000, "cb-d")
        call(svc, "reg", "record_event",
             {"contract_id": "c1", "event_type": "structure_top"})
        call(svc, "reg", "record_event",
             {"contract_id": "c1", "event_type": "acceptance"})
        r = call(svc, "reg", "record_event",
                 {"contract_id": "c1", "event_type": "cancel_buyer"})
        self.assertFalse(r.accepted)


if __name__ == "__main__":
    unittest.main()
