"""资金追溯：fund_trail 还原每笔购房款从入账到最终去向的完整依据。"""
import unittest

from tests.world import (call, fresh, pending_instructions, settle_in,
                         settle_out, sign_default)


def trail(svc, contract="c1", actor="reg"):
    r = call(svc, actor, "fund_trail", {"contract_id": contract})
    assert r.accepted, r.message
    return r.data


class FundTrailHappyPathTest(unittest.TestCase):
    def test_full_chain_has_bank_receipts_and_event_basis(self):
        svc, _ = fresh()
        sign_default(svc)
        settle_in(svc, "bank1", "c1", 1, 30_000, "cb-dep")
        settle_in(svc, "bank1", "c1", 2, 970_000, "cb-rest")
        call(svc, "reg", "record_event",
             {"contract_id": "c1", "event_type": "structure_top"})
        top = pending_instructions(svc)[0]
        settle_out(svc, "bank1", top["id"], "cb-out-top")
        call(svc, "reg", "record_event",
             {"contract_id": "c1", "event_type": "acceptance"})
        acc = pending_instructions(svc)[0]
        settle_out(svc, "bank1", acc["id"], "cb-out-acc")

        t = trail(svc)
        # 两笔入账都有银行回单号可查
        self.assertEqual([i["callback_id"] for i in t["inflows"]],
                         ["cb-dep", "cb-rest"])
        # 两笔出账都能追溯到节点事件依据与确认人
        bases = {(o["flow_type"], o["basis"]["event_type"],
                  o["basis"]["confirmed_by"]) for o in t["outflows"]}
        self.assertIn(("out_developer", "structure_top", "reg"), bases)
        self.assertIn(("out_developer", "acceptance", "reg"), bases)
        # 每笔出账都有银行回单
        self.assertTrue(all(o["callback_id"] for o in t["outflows"]))
        # 勾稽：入 100 万 = 出 100 万，留存 0
        rec = t["reconciliation"]
        self.assertEqual(rec["total_in_yuan"], "1000000.00")
        self.assertEqual(rec["escrow_out_settled_yuan"], "1000000.00")
        self.assertEqual(rec["retained_for_contract_yuan"], "0.00")
        self.assertTrue(rec["balanced"])
        # 分类账完整有序
        self.assertEqual([(e["direction"], e["amount_yuan"]) for e in t["ledger"]],
                         [("in", "30000.00"), ("in", "970000.00"),
                          ("out", "600000.00"), ("out", "400000.00")])

    def test_buyer_trail_hides_account_level_balance(self):
        svc, _ = fresh()
        sign_default(svc)
        settle_in(svc, "bank1", "c1", 1, 30_000, "cb-dep")
        t = trail(svc, actor="buyer1")
        self.assertIsNone(t["ledger"][0]["account_balance_after_yuan"])
        self.assertEqual(t["ledger"][0]["contract_retained_after_yuan"], "30000.00")


class FundTrailTerminationTest(unittest.TestCase):
    def test_overdue_trail_links_refund_to_overdue_event(self):
        svc, _ = fresh()
        sign_default(svc)
        settle_in(svc, "bank1", "c1", 1, 30_000, "cb-dep")
        settle_in(svc, "bank1", "c1", 2, 970_000, "cb-rest")
        call(svc, "reg", "record_event",
             {"contract_id": "c1", "event_type": "structure_top"})
        settle_out(svc, "bank1", pending_instructions(svc)[0]["id"], "cb-top")
        call(svc, "reg", "record_event",
             {"contract_id": "c1", "event_type": "overdue"})
        refund = pending_instructions(svc)[0]
        settle_out(svc, "bank1", refund["id"], "cb-ref")

        t = trail(svc)
        out = {o["flow_type"]: o for o in t["outflows"]}
        self.assertEqual(out["out_developer"]["basis"]["event_type"], "structure_top")
        self.assertEqual(out["out_buyer"]["basis"]["event_type"], "overdue")
        self.assertEqual(t["reconciliation"]["retained_for_contract_yuan"], "0.00")


if __name__ == "__main__":
    unittest.main()
