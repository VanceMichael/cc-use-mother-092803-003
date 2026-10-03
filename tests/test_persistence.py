"""SQLite 持久化：重启进程后数据、幂等记录与资金状态完整保留。"""
import os
import tempfile
import unittest

from app.service import HousingEscrowService
from app.store import Store
from tests.world import call, settle_in, sign_default


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        os.unlink(self.path)

    def test_state_survives_reopen(self):
        store = Store(self.path)
        svc = HousingEscrowService(store)
        # 用 world.fresh 的夹具逻辑，但落到文件库
        from tests import world
        with store.tx() as c:
            c.execute("INSERT INTO actors (actor_id, role, display_name, scope_id)"
                      " VALUES ('reg', 'regulator', '监管员', NULL)")
        call(svc, "reg", "register_developer", {"id": "d1", "name": "甲地产"})
        call(svc, "reg", "register_bank", {"id": "b1", "name": "甲行"})
        call(svc, "reg", "create_project",
             {"id": "p1", "developer_id": "d1", "bank_id": "b1", "name": "院"})
        call(svc, "reg", "open_account", {"project_id": "p1", "account_no": "A1"})
        call(svc, "reg", "add_unit",
             {"id": "u1", "project_id": "p1", "unit_no": "1", "price": 1_000_000})
        call(svc, "reg", "register_buyer",
             {"id": "buyer1", "name": "张三", "id_no": "110"})
        call(svc, "reg", "register_actor",
             {"id": "bank1", "role": "bank", "scope_id": "b1"})
        call(svc, "reg", "register_actor",
             {"id": "dev1", "role": "developer", "scope_id": "d1"})
        sign_default(svc)
        settle_in(svc, "bank1", "c1", 1, 30_000, "cb-d")
        store.close()

        # —— 重新打开，如同服务重启 ——
        store2 = Store(self.path)
        svc2 = HousingEscrowService(store2)
        r = call(svc2, "reg", "get_account", {"project_id": "p1"})
        self.assertEqual(r.data["account"]["balance_yuan"], "30000.00")
        r = call(svc2, "reg", "get_contract", {"contract_id": "c1"})
        self.assertEqual(r.data["state"], "active")

        # 幂等记录也持久化：原回调换 request_id 仍按业务幂等去重
        again = settle_in(svc2, "bank1", "c1", 1, 30_000, "cb-d-2")
        self.assertTrue(again.data.get("deduplicated"))
        store2.close()
