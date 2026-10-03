"""预售资金监管后端行为测试。

覆盖：定金 3% 上限、节点顺序、提前放款拦截、解冻全流程、
银行回调重送去重、角色与项目范围隔离、四类终态结算、资金全链路还原。
"""
import unittest

from app.contracts import Request
from app.service import HousingEscrowService
from app.store import Store

PRICE = 10_000_000          # 备案总价 10,000,000 分
DEPOSIT_CAP = PRICE * 3 // 100   # 300,000 分


class EscrowTestBase(unittest.TestCase):
    def setUp(self):
        self.service = HousingEscrowService(Store(":memory:"))
        self._seq = 0
        self._bootstrap()

    def call(self, actor, action, payload=None):
        self._seq += 1
        return self.service.handle(
            Request(actor, action, payload or {}, f"req-{self._seq}"))

    def call_raw(self, actor, action, payload, request_id):
        return self.service.handle(Request(actor, action, payload, request_id))

    def _bootstrap(self):
        # 管理员
        self.assertTrue(self.call("admin", "create_user", {
            "username": "admin", "role": "admin"}).accepted)
        for username, role, project in [
            ("dev1", "developer", "P1"),
            ("reg1", "regulator", "P1"),
            ("bank1", "bank", "P1"),
            ("dev2", "developer", "P2"),
            ("reg2", "regulator", "P2"),
            ("bank2", "bank", "P2"),
            ("auditor", "auditor", None),
        ]:
            self.assertTrue(self.call("admin", "create_user", {
                "username": username, "role": role,
                "project_id": project}).accepted)
        self.assertTrue(self.call("admin", "create_user", {
            "username": "buyer1", "role": "buyer", "buyer_id": "B1"}).accepted)
        self.assertTrue(self.call("admin", "create_user", {
            "username": "buyer2", "role": "buyer", "buyer_id": "B2"}).accepted)

        self.assertTrue(self.call("admin", "create_project", {
            "project_id": "P1", "name": "春风里一期"}).accepted)
        self.assertTrue(self.call("admin", "create_project", {
            "project_id": "P2", "name": "秋月苑"}).accepted)
        self.assertTrue(self.call("admin", "create_account", {
            "account_no": "ACC1", "project_id": "P1", "bank_name": "监管银行"}).accepted)
        self.assertTrue(self.call("admin", "create_unit", {
            "project_id": "P1", "unit_no": "U1", "building": "1栋",
            "total_price": PRICE}).accepted)

    def sign(self, contract_no="C1", buyer="B1", deposit=DEPOSIT_CAP,
             down=2_700_000, mortgage=7_000_000, unit_no="U1", actor="dev1"):
        return self.call(actor, "sign_contract", {
            "contract_no": contract_no, "project_id": "P1", "unit_no": unit_no,
            "buyer_id": buyer, "buyer_name": "购房人" + buyer,
            "deposit": deposit, "down_payment": down, "mortgage": mortgage})

    def fund(self, contract_no="C1", start=0):
        """银行回调三笔资金全部入账，返回流水号列表。"""
        serials = []
        for i, (fee, amount) in enumerate([
                ("deposit", DEPOSIT_CAP),
                ("down_payment", 2_700_000),
                ("mortgage", 7_000_000)]):
            serial = f"IN-{contract_no}-{fee}"
            serials.append(serial)
            result = self.call("bank1", "bank_inbound", {
                "contract_no": contract_no, "fee_type": fee,
                "amount": amount, "bank_serial": serial})
            self.assertTrue(result.accepted, msg=result.message)
        return serials

    def balance(self):
        return self.service.store.query_one(
            "SELECT balance FROM escrow_accounts WHERE account_no='ACC1'")["balance"]


class DepositRuleTest(EscrowTestBase):
    def test_deposit_at_cap_accepted(self):
        result = self.sign(deposit=DEPOSIT_CAP)
        self.assertTrue(result.accepted)
        self.assertEqual(result.data["deposit_cap"], DEPOSIT_CAP)

    def test_deposit_over_cap_rejected(self):
        result = self.sign(deposit=DEPOSIT_CAP + 1)
        self.assertFalse(result.accepted)
        self.assertEqual(result.state, "deposit_cap_exceeded")

    def test_amounts_must_equal_total_price(self):
        result = self.sign(deposit=100_000, down=2_000_000, mortgage=7_000_000)
        self.assertFalse(result.accepted)
        self.assertEqual(result.state, "amount_mismatch")

    def test_rejected_request_is_idempotent_and_side_effect_free(self):
        payload = {"contract_no": "C9", "project_id": "P1", "unit_no": "U1",
                   "buyer_id": "B1", "deposit": DEPOSIT_CAP + 1,
                   "down_payment": 1_000_000, "mortgage": 0}
        first = self.call_raw("dev1", "sign_contract", payload, "fixed-rid")
        second = self.call_raw("dev1", "sign_contract", payload, "fixed-rid")
        self.assertFalse(first.accepted)
        self.assertEqual(first, second)
        self.assertIsNone(self.service.store.query_one(
            "SELECT 1 FROM contracts WHERE contract_no='C9'"))


class NodeAndReleaseTest(EscrowTestBase):
    def test_node_must_follow_order(self):
        result = self.call("reg1", "attest_node",
                           {"project_id": "P1", "node": "acceptance"})
        self.assertFalse(result.accepted)
        self.assertEqual(result.state, "node_out_of_order")

    def test_release_before_attestation_blocked(self):
        self.assertTrue(self.sign().accepted)
        self.fund()
        result = self.call("dev1", "request_release", {
            "contract_no": "C1", "node": "floor_topout", "amount": 1_000_000})
        self.assertFalse(result.accepted)
        self.assertEqual(result.state, "node_not_attested")
        self.assertEqual(self.balance(), PRICE)  # 钱一分没动

    def test_release_over_node_quota_blocked(self):
        self.assertTrue(self.sign().accepted)
        self.fund()
        self.assertTrue(self.call("reg1", "attest_node", {
            "project_id": "P1", "node": "floor_topout"}).accepted)
        # 封顶累计可解冻 60% = 6,000,000；申请 7,000,000 必须拒绝
        result = self.call("dev1", "request_release", {
            "contract_no": "C1", "node": "floor_topout", "amount": 7_000_000})
        self.assertFalse(result.accepted)
        self.assertEqual(result.state, "quota_exceeded")

    def test_full_release_flow_with_fifo_buckets(self):
        self.assertTrue(self.sign().accepted)
        self.fund()
        self.assertTrue(self.call("reg1", "attest_node", {
            "project_id": "P1", "node": "floor_topout"}).accepted)

        requested = self.call("dev1", "request_release", {
            "contract_no": "C1", "node": "floor_topout", "amount": 6_000_000})
        self.assertTrue(requested.accepted)
        outgo_id = requested.data["outgo_id"]

        # 开发商/银行不能自行审批
        self.assertFalse(self.call("dev1", "approve_release",
                                   {"outgo_id": outgo_id}).accepted)
        approved = self.call("reg1", "approve_release", {"outgo_id": outgo_id})
        self.assertEqual(approved.state, "approved")

        # 未审批通过，银行不能划款（用第二套合同验证 pending 闸门）
        self.assertTrue(self.call("admin", "create_unit", {
            "project_id": "P1", "unit_no": "U2", "building": "1栋",
            "total_price": PRICE}).accepted)
        self.assertTrue(self.sign(contract_no="C2", unit_no="U2").accepted)
        pending_req = self.call("dev1", "request_release", {
            "contract_no": "C2", "node": "floor_topout", "amount": 100})
        pending_id = pending_req.data["outgo_id"]
        not_approved = self.call("bank1", "bank_outbound_result", {
            "outgo_id": pending_id, "bank_serial": "OUT-EARLY", "success": True})
        self.assertFalse(not_approved.accepted)
        self.assertEqual(not_approved.state, "not_approved")
        self.assertTrue(self.call("reg1", "reject_release", {
            "outgo_id": pending_id, "reason": "测试占用额度，驳回"}).accepted)

        paid = self.call("bank1", "bank_outbound_result", {
            "outgo_id": outgo_id, "bank_serial": "OUT-1", "success": True})
        self.assertTrue(paid.accepted)
        self.assertEqual(paid.state, "paid")
        # FIFO：定金 30 万 + 首付 270 万 + 按揭 300 万
        breakdown = {(b["fee_type"]): b["amount"] for b in paid.data["breakdown"]}
        self.assertEqual(breakdown, {"deposit": 300_000,
                                     "down_payment": 2_700_000,
                                     "mortgage": 3_000_000})
        self.assertEqual(self.balance(), 4_000_000)

        # 验收后额度提高到 100%，剩余 400 万可拨
        self.assertTrue(self.call("reg1", "attest_node", {
            "project_id": "P1", "node": "acceptance"}).accepted)
        rest = self.call("dev1", "request_release", {
            "contract_no": "C1", "node": "acceptance", "amount": 4_000_000})
        self.assertTrue(rest.accepted)
        rid = rest.data["outgo_id"]
        self.assertTrue(self.call("reg1", "approve_release",
                                  {"outgo_id": rid}).accepted)
        paid2 = self.call("bank1", "bank_outbound_result", {
            "outgo_id": rid, "bank_serial": "OUT-2", "success": True})
        self.assertEqual(paid2.state, "paid")
        self.assertEqual(self.balance(), 0)

    def test_rejected_application_releases_quota(self):
        self.assertTrue(self.sign().accepted)
        self.fund()
        self.assertTrue(self.call("reg1", "attest_node", {
            "project_id": "P1", "node": "floor_topout"}).accepted)
        first = self.call("dev1", "request_release", {
            "contract_no": "C1", "node": "floor_topout", "amount": 6_000_000})
        self.assertTrue(self.call("reg1", "reject_release", {
            "outgo_id": first.data["outgo_id"], "reason": "资料不齐"}).accepted)
        again = self.call("dev1", "request_release", {
            "contract_no": "C1", "node": "floor_topout", "amount": 6_000_000})
        self.assertTrue(again.accepted, msg=again.message)


class BankCallbackTest(EscrowTestBase):
    def test_inbound_amount_mismatch_rejected_without_ledger(self):
        self.assertTrue(self.sign().accepted)
        result = self.call("bank1", "bank_inbound", {
            "contract_no": "C1", "fee_type": "deposit",
            "amount": DEPOSIT_CAP - 1, "bank_serial": "IN-BAD"})
        self.assertFalse(result.accepted)
        self.assertEqual(result.state, "amount_mismatch")
        self.assertEqual(self.balance(), 0)

    def test_inbound_callback_resend_is_deduped(self):
        self.assertTrue(self.sign().accepted)
        payload = {"contract_no": "C1", "fee_type": "deposit",
                   "amount": DEPOSIT_CAP, "bank_serial": "IN-DUP"}
        first = self.call("bank1", "bank_inbound", payload)
        self.assertTrue(first.accepted)
        # 即使换了 request_id，同一银行流水重送也不能重复入账
        second = self.call_raw("bank1", "bank_inbound", payload, "another-rid")
        self.assertTrue(second.accepted)
        self.assertEqual(second.state, "duplicate_callback")
        self.assertEqual(self.balance(), DEPOSIT_CAP)

    def test_outbound_callback_resend_does_not_pay_twice(self):
        self.assertTrue(self.sign().accepted)
        self.fund()
        self.assertTrue(self.call("reg1", "attest_node", {
            "project_id": "P1", "node": "floor_topout"}).accepted)
        req = self.call("dev1", "request_release", {
            "contract_no": "C1", "node": "floor_topout", "amount": 1_000_000})
        outgo_id = req.data["outgo_id"]
        self.assertTrue(self.call("reg1", "approve_release",
                                  {"outgo_id": outgo_id}).accepted)
        payload = {"outgo_id": outgo_id, "bank_serial": "OUT-DUP", "success": True}
        first = self.call("bank1", "bank_outbound_result", payload)
        self.assertEqual(first.state, "paid")
        self.assertEqual(self.balance(), PRICE - 1_000_000)
        # 回调重送（新 request_id、同流水号）
        rows_before = self.service.store.query_one(
            "SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS s"
            " FROM ledger_entries WHERE bank_serial='OUT-DUP'")
        second = self.call_raw("bank1", "bank_outbound_result", payload, "rid-resend")
        rows_after = self.service.store.query_one(
            "SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS s"
            " FROM ledger_entries WHERE bank_serial='OUT-DUP'")
        self.assertTrue(second.data.get("duplicate"))
        self.assertEqual(self.balance(), PRICE - 1_000_000)
        # 重送不产生任何新台账行：笔数、总额完全一致
        self.assertEqual(tuple(rows_before), tuple(rows_after))
        self.assertEqual(rows_after["s"], 1_000_000)

    def test_failed_outbound_keeps_funds_in_escrow(self):
        self.assertTrue(self.sign().accepted)
        self.fund()
        self.assertTrue(self.call("reg1", "attest_node", {
            "project_id": "P1", "node": "floor_topout"}).accepted)
        req = self.call("dev1", "request_release", {
            "contract_no": "C1", "node": "floor_topout", "amount": 1_000_000})
        outgo_id = req.data["outgo_id"]
        self.assertTrue(self.call("reg1", "approve_release",
                                  {"outgo_id": outgo_id}).accepted)
        failed = self.call("bank1", "bank_outbound_result", {
            "outgo_id": outgo_id, "bank_serial": "OUT-FAIL",
            "success": False, "fail_reason": "账户异常"})
        self.assertEqual(failed.state, "failed")
        self.assertEqual(self.balance(), PRICE)


class RbacTest(EscrowTestBase):
    def test_role_separation(self):
        self.assertEqual(self.call("dev1", "attest_node", {
            "project_id": "P1", "node": "floor_topout"}).state, "forbidden")
        self.assertEqual(self.call("reg1", "sign_contract", {
            "project_id": "P1", "unit_no": "U1"}).state, "forbidden")
        self.assertEqual(self.call("bank1", "create_project", {
            "project_id": "PX", "name": "x"}).state, "forbidden")
        self.assertEqual(self.call("auditor", "create_project", {
            "project_id": "PY", "name": "y"}).state, "forbidden")

    def test_project_scope_enforced(self):
        # P2 的开发商不能在 P1 备案房源/签约
        result = self.call("admin", "create_unit", {
            "project_id": "P2", "unit_no": "U9", "total_price": PRICE})
        self.assertTrue(result.accepted)
        cross = self.call("dev2", "create_unit", {
            "project_id": "P1", "unit_no": "UX", "total_price": PRICE})
        self.assertEqual(cross.state, "out_of_scope")

        self.assertTrue(self.sign().accepted)
        self.assertEqual(self.call("bank2", "bank_inbound", {
            "contract_no": "C1", "fee_type": "deposit",
            "amount": DEPOSIT_CAP, "bank_serial": "IN-X"}).state, "out_of_scope")
        self.assertEqual(self.call("reg2", "attest_node", {
            "project_id": "P1", "node": "floor_topout"}).state, "out_of_scope")

    def test_buyer_sees_only_own_contract(self):
        self.assertTrue(self.sign(contract_no="C1", buyer="B1").accepted)
        own = self.call("buyer1", "fund_trace", {"contract_no": "C1"})
        self.assertTrue(own.accepted)
        other = self.call("buyer2", "fund_trace", {"contract_no": "C1"})
        self.assertEqual(other.state, "out_of_scope")

    def test_overview_is_scoped(self):
        result = self.call("dev2", "project_overview", {"project_id": "P1"})
        self.assertEqual(result.state, "out_of_scope")
        own = self.call("dev1", "project_overview", {})
        ids = [p["project_id"] for p in own.data["projects"]]
        self.assertEqual(ids, ["P1"])
        all_projects = self.call("auditor", "project_overview", {})
        self.assertEqual(len(all_projects.data["projects"]), 2)


class SettlementTest(EscrowTestBase):
    def _outgo_ids(self, contract_no, kind):
        rows = self.service.store.query_all(
            "SELECT id FROM outgo_items WHERE contract_no=? AND kind=? ORDER BY id",
            (contract_no, kind))
        return [r["id"] for r in rows]

    def _pay_all(self, ids, serial_prefix):
        for i, oid in enumerate(ids):
            result = self.call("bank1", "bank_outbound_result", {
                "outgo_id": oid, "bank_serial": f"{serial_prefix}-{i}", "success": True})
            self.assertTrue(result.accepted, msg=result.message)

    def test_cancelled_refunds_everything(self):
        self.assertTrue(self.sign().accepted)
        self.fund()
        event = self.call("reg1", "register_event", {
            "contract_no": "C1", "event": "cancelled", "reason": "协商解除"})
        self.assertEqual(event.state, "cancelled")
        ids = self._outgo_ids("C1", "refund")
        self.assertEqual(len(ids), 3)
        self._pay_all(ids, "REF")
        self.assertEqual(self.balance(), 0)
        # 终态合同不能再发起解冻
        again = self.call("dev1", "request_release", {
            "contract_no": "C1", "node": "floor_topout", "amount": 1})
        self.assertEqual(again.state, "contract_terminal")

    def test_buyer_breach_forfeits_deposit_only(self):
        self.assertTrue(self.sign().accepted)
        self.fund()
        self.assertTrue(self.call("reg1", "register_event", {
            "contract_no": "C1", "event": "buyer_breach"}).accepted)
        forfeits = self._outgo_ids("C1", "forfeit")
        refunds = self._outgo_ids("C1", "refund")
        self.assertEqual(len(forfeits), 1)
        self.assertEqual(len(refunds), 2)  # 首付、按揭退还
        self._pay_all(forfeits, "FOR")
        self._pay_all(refunds, "REF")
        self.assertEqual(self.balance(), 0)
        trace = self.call("buyer1", "fund_trace", {"contract_no": "C1"})
        s = trace.data["summary_by_fee"]
        self.assertEqual(s["deposit"], {"received": 300_000, "released": 0,
                                        "refunded": 0, "forfeited": 300_000, "held": 0})

    def test_developer_breach_refunds_including_deposit(self):
        self.assertTrue(self.sign().accepted)
        self.fund()
        self.assertTrue(self.call("reg1", "register_event", {
            "contract_no": "C1", "event": "developer_breach"}).accepted)
        self.assertEqual(self._outgo_ids("C1", "forfeit"), [])
        ids = self._outgo_ids("C1", "refund")
        self.assertEqual(len(ids), 3)
        self._pay_all(ids, "REF")
        self.assertEqual(self.balance(), 0)

    def test_overdue_after_partial_release_only_refunds_held_funds(self):
        self.assertTrue(self.sign().accepted)
        self.fund()
        self.assertTrue(self.call("reg1", "attest_node", {
            "project_id": "P1", "node": "floor_topout"}).accepted)
        req = self.call("dev1", "request_release", {
            "contract_no": "C1", "node": "floor_topout", "amount": 6_000_000})
        oid = req.data["outgo_id"]
        self.assertTrue(self.call("reg1", "approve_release",
                                  {"outgo_id": oid}).accepted)
        self.assertTrue(self.call("bank1", "bank_outbound_result", {
            "outgo_id": oid, "bank_serial": "OUT-1", "success": True}).accepted)
        self.assertEqual(self.balance(), 4_000_000)

        self.assertTrue(self.call("reg1", "register_event", {
            "contract_no": "C1", "event": "overdue"}).accepted)
        ids = self._outgo_ids("C1", "refund")
        self.assertEqual(len(ids), 1)  # 只剩按揭桶里 400 万在监管账户
        self._pay_all(ids, "REF")
        self.assertEqual(self.balance(), 0)  # 已拨付的 600 万不追回

    def test_approved_but_unpaid_release_blocked_after_terminal_event(self):
        self.assertTrue(self.sign().accepted)
        self.fund()
        self.assertTrue(self.call("reg1", "attest_node", {
            "project_id": "P1", "node": "floor_topout"}).accepted)
        req = self.call("dev1", "request_release", {
            "contract_no": "C1", "node": "floor_topout", "amount": 6_000_000})
        oid = req.data["outgo_id"]
        self.assertTrue(self.call("reg1", "approve_release",
                                  {"outgo_id": oid}).accepted)
        # 合同终止：已批准未划款的拨款必须被拦下
        self.assertTrue(self.call("reg1", "register_event", {
            "contract_no": "C1", "event": "overdue"}).accepted)
        blocked = self.call("bank1", "bank_outbound_result", {
            "outgo_id": oid, "bank_serial": "OUT-LATE", "success": True})
        self.assertFalse(blocked.accepted)
        self.assertEqual(blocked.state, "contract_terminal")
        self.assertEqual(self.balance(), PRICE)  # 一分未拨
        # 随后结算全额退款
        self._pay_all(self._outgo_ids("C1", "refund"), "REF")
        self.assertEqual(self.balance(), 0)

    def test_pending_applications_rejected_on_settlement(self):
        self.assertTrue(self.sign().accepted)
        self.fund()
        self.assertTrue(self.call("reg1", "attest_node", {
            "project_id": "P1", "node": "floor_topout"}).accepted)
        req = self.call("dev1", "request_release", {
            "contract_no": "C1", "node": "floor_topout", "amount": 1_000_000})
        oid = req.data["outgo_id"]
        self.assertTrue(self.call("reg1", "register_event", {
            "contract_no": "C1", "event": "cancelled"}).accepted)
        status = self.service.store.query_one(
            "SELECT status FROM outgo_items WHERE id=?", (oid,))["status"]
        self.assertEqual(status, "rejected")


class FundTraceTest(EscrowTestBase):
    def test_trace_reconstructs_full_journey(self):
        self.assertTrue(self.sign().accepted)
        self.fund()
        self.assertTrue(self.call("reg1", "attest_node", {
            "project_id": "P1", "node": "floor_topout"}).accepted)
        req = self.call("dev1", "request_release", {
            "contract_no": "C1", "node": "floor_topout", "amount": 6_000_000})
        oid = req.data["outgo_id"]
        self.assertTrue(self.call("reg1", "approve_release",
                                  {"outgo_id": oid}).accepted)
        self.assertTrue(self.call("bank1", "bank_outbound_result", {
            "outgo_id": oid, "bank_serial": "OUT-1", "success": True}).accepted)
        self.assertTrue(self.call("reg1", "register_event", {
            "contract_no": "C1", "event": "overdue"}).accepted)
        refunds = self.service.store.query_all(
            "SELECT id FROM outgo_items WHERE contract_no='C1' AND kind='refund'")
        for i, row in enumerate(refunds):
            self.assertTrue(self.call("bank1", "bank_outbound_result", {
                "outgo_id": row["id"], "bank_serial": f"REF-{i}",
                "success": True}).accepted)

        trace = self.call("auditor", "fund_trace", {"contract_no": "C1"})
        self.assertTrue(trace.accepted)
        # 逐笔台账：3 笔入账 + 拨款拆成 3 桶 + 1 笔退款 = 7
        self.assertEqual(len(trace.data["trail"]), 7)
        summary = trace.data["summary_by_fee"]
        total_in = sum(v["received"] for v in summary.values())
        total_out = sum(v["released"] + v["refunded"] + v["forfeited"]
                        for v in summary.values())
        total_held = sum(v["held"] for v in summary.values())
        self.assertEqual(total_in, PRICE)
        self.assertEqual(total_out, PRICE)
        self.assertEqual(total_held, 0)
        # 台账不可变、只追加：所有移动都能在 trail 中找到流水号与依据
        for entry in trace.data["trail"]:
            self.assertTrue(entry["bank_serial"])
            self.assertTrue(entry["reason"])


if __name__ == "__main__":
    unittest.main()
