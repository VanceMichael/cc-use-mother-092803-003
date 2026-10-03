"""测试夹具：构造一个含两个项目公司、两家银行、两个项目的监管世界。"""
from __future__ import annotations

import uuid

from app.contracts import Request
from app.service import HousingEscrowService
from app.store import Store


def call(svc: HousingEscrowService, actor: str, action: str, payload: dict,
         rid: str | None = None):
    return svc.handle(Request(actor, action, payload, rid or f"rid-{uuid.uuid4().hex}"))


def fresh(path: str = ":memory:"):
    store = Store(path)
    svc = HousingEscrowService(store)
    # 内置监管账号直接落库（生产中由 api.bootstrap 完成）
    with store.tx() as c:
        c.execute("INSERT INTO actors (actor_id, role, display_name, scope_id)"
                  " VALUES ('reg', 'regulator', '监管员', NULL)")

    call(svc, "reg", "register_developer", {"id": "d1", "name": "甲地产"})
    call(svc, "reg", "register_developer", {"id": "d2", "name": "乙地产"})
    call(svc, "reg", "register_bank", {"id": "b1", "name": "甲监管银行"})
    call(svc, "reg", "register_bank", {"id": "b2", "name": "乙监管银行"})

    call(svc, "reg", "create_project",
         {"id": "p1", "developer_id": "d1", "bank_id": "b1", "name": "甲一号院"})
    call(svc, "reg", "create_project",
         {"id": "p2", "developer_id": "d2", "bank_id": "b2", "name": "乙二号院"})
    call(svc, "reg", "open_account",
         {"project_id": "p1", "account_no": "6221-P1"})
    call(svc, "reg", "open_account",
         {"project_id": "p2", "account_no": "6222-P2"})
    call(svc, "reg", "add_unit",
         {"id": "u1", "project_id": "p1", "unit_no": "1-101", "price": 1_000_000})
    call(svc, "reg", "add_unit",
         {"id": "u2", "project_id": "p2", "unit_no": "2-202", "price": 2_000_000})
    call(svc, "reg", "register_buyer",
         {"id": "buyer1", "name": "张三", "id_no": "110101", "bank_account": "6217-B1"})
    call(svc, "reg", "register_buyer",
         {"id": "buyer2", "name": "李四", "id_no": "110102", "bank_account": "6217-B2"})

    for actor_id, role, scope in [
        ("dev1", "developer", "d1"), ("dev2", "developer", "d2"),
        ("bank1", "bank", "b1"), ("bank2", "bank", "b2"),
        ("buyer1", "buyer", "buyer1"), ("buyer2", "buyer", "buyer2"),
    ]:
        call(svc, "reg", "register_actor",
             {"id": actor_id, "role": role, "scope_id": scope,
              "display_name": actor_id})
    return svc, store


def sign_default(svc, contract_id="c1", unit="u1", buyer="buyer1",
                 project="p1", deposit=30_000, total=1_000_000, actor="dev1"):
    return call(svc, actor, "sign_contract", {
        "id": contract_id, "contract_no": f"HT-{contract_id}",
        "project_id": project, "unit_id": unit, "buyer_id": buyer,
        "deposit": deposit, "total_price": total})


def settle_in(svc, bank, contract, seq, amount, cb_id, project="p1"):
    return call(svc, bank, "bank_callback", {
        "callback_id": cb_id, "direction": "in",
        "project_id": project, "contract_id": contract,
        "seq": seq, "amount": amount, "bank_ref": f"REF-{cb_id}"})


def pending_instructions(svc, actor="bank1", project=None):
    payload = {"status": "pending"}
    if project:
        payload["project_id"] = project
    r = call(svc, actor, "list_instructions", payload)
    assert r.accepted, r.message
    return r.data["instructions"]


def settle_out(svc, bank, instr_id, cb_id):
    return call(svc, bank, "bank_callback", {
        "callback_id": cb_id, "instr_id": instr_id,
        "status": "settled", "bank_ref": f"REF-{cb_id}"})


def balance(svc, project="p1", actor="reg"):
    r = call(svc, actor, "get_account", {"project_id": project})
    assert r.accepted, r.message
    return r.data["account"]["balance_yuan"]
