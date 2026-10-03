"""角色与数据范围：监管/项目公司/银行/购房人只能看和处理职责范围内的数据。"""
import unittest

from tests.world import call, fresh, settle_in, sign_default


class RbacTest(unittest.TestCase):
    def setUp(self):
        self.svc, _ = fresh()
        sign_default(self.svc)
        settle_in(self.svc, "bank1", "c1", 1, 30_000, "cb-d")
        # 第二个项目上也签一份合同
        sign_default(self.svc, "c2", unit="u2", buyer="buyer2",
                     project="p2", deposit=60_000, total=2_000_000, actor="dev2")
        settle_in(self.svc, "bank2", "c2", 1, 60_000, "cb-d2", project="p2")

    def test_developer_cannot_touch_other_company_project(self):
        r = call(self.svc, "dev2", "add_unit",
                 {"id": "ux", "project_id": "p1", "unit_no": "x", "price": 10})
        self.assertFalse(r.accepted)
        self.assertEqual(r.state, "forbidden")

    def test_bank_cannot_handle_other_bank_project(self):
        r = call(self.svc, "bank2", "bank_callback", {
            "callback_id": "cb-x", "direction": "in",
            "project_id": "p1", "contract_id": "c1",
            "seq": 2, "amount": 970_000})
        self.assertFalse(r.accepted)
        self.assertEqual(r.state, "forbidden")

    def test_buyer_only_sees_own_contract(self):
        ok = call(self.svc, "buyer1", "get_contract", {"contract_id": "c1"})
        self.assertTrue(ok.accepted)
        denied = call(self.svc, "buyer1", "get_contract", {"contract_id": "c2"})
        self.assertFalse(denied.accepted)
        self.assertEqual(denied.state, "forbidden")

    def test_buyer_cannot_view_whole_account(self):
        r = call(self.svc, "buyer1", "get_account", {"project_id": "p1"})
        self.assertFalse(r.accepted)

    def test_buyer_cannot_record_events(self):
        r = call(self.svc, "buyer1", "record_event",
                 {"contract_id": "c1", "event_type": "structure_top"})
        self.assertFalse(r.accepted)

    def test_developer_cannot_confirm_structure_top(self):
        r = call(self.svc, "dev1", "record_event",
                 {"contract_id": "c1", "event_type": "structure_top"})
        self.assertFalse(r.accepted)

    def test_bank_lists_only_own_instructions(self):
        call(self.svc, "reg", "record_event",
             {"contract_id": "c1", "event_type": "structure_top"})
        call(self.svc, "reg", "record_event",
             {"contract_id": "c2", "event_type": "structure_top"})
        r1 = call(self.svc, "bank1", "list_instructions", {})
        r2 = call(self.svc, "bank2", "list_instructions", {})
        projects_1 = {i["project_id"] for i in r1.data["instructions"]}
        projects_2 = {i["project_id"] for i in r2.data["instructions"]}
        self.assertEqual(projects_1, {"p1"})
        self.assertEqual(projects_2, {"p2"})

    def test_regulator_sees_all_projects(self):
        for pid in ("p1", "p2"):
            r = call(self.svc, "reg", "get_project", {"project_id": pid})
            self.assertTrue(r.accepted)

    def test_unknown_actor_rejected(self):
        r = call(self.svc, "ghost", "get_contract", {"contract_id": "c1"})
        self.assertFalse(r.accepted)
        self.assertEqual(r.state, "forbidden")


if __name__ == "__main__":
    unittest.main()
