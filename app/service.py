"""预售资金监管领域服务。

资金一律以整数"分"记录；监管账户是核心闸门：
- 入账：银行回调把定金/房款划入监管账户；
- 出账：只有节点事件（封顶/验收/违约/取消/逾期）才会生成出金指令，银行凭指令划款；
- 银行回调以 callback_id 去重，指令以 idem_key 去重，请求以 request_id 去重，重送绝不重复划款。
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from . import contracts as C
from .store import Store, from_cents, now, to_cents

BOOTSTRAP_REGULATOR = "regulator"


def bootstrap(store: Store) -> str | None:
    """库为空时创建内置监管账号并签发 token；账号已存在则返回 None。"""
    import hashlib
    import secrets
    conn = store.conn
    if conn.execute("SELECT 1 FROM actors WHERE actor_id = ?",
                    (BOOTSTRAP_REGULATOR,)).fetchone():
        return None
    plaintext = secrets.token_urlsafe(24)
    digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    with store.tx() as c:
        c.execute(
            "INSERT INTO actors (actor_id, role, display_name, scope_id)"
            " VALUES (?, 'regulator', '内置监管人员', NULL)",
            (BOOTSTRAP_REGULATOR,))
        c.execute(
            "INSERT INTO api_tokens (token, actor_id, issued_at, revoked)"
            " VALUES (?, ?, ?, 0)", (digest, BOOTSTRAP_REGULATOR, now()))
    return plaintext


class HousingEscrowService:
    def __init__(self, store: Store) -> None:
        self.store = store
        # 动作 -> 处理器（统一签名 (conn, actor, payload, request_id) -> dict）
        self._handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "register_developer": self._register_developer,
            "register_bank": self._register_bank,
            "register_actor": self._register_actor,
            "create_project": self._create_project,
            "open_account": self._open_account,
            "add_unit": self._add_unit,
            "register_buyer": self._register_buyer,
            "sign_contract": self._sign_contract,
            "record_event": self._record_event,
            "bank_callback": self._bank_callback,
            "get_contract": self._get_contract,
            "get_project": self._get_project,
            "get_account": self._get_account,
            "list_instructions": self._list_instructions,
            "fund_trail": self._fund_trail,
            "issue_token": self._issue_token,
            "revoke_token": self._revoke_token,
        }

    # ============================== 入口 ==============================
    def handle(self, request: C.Request) -> C.Result:
        C.validate_request(request)
        actor_id = request.actor
        handler = self._handlers.get(request.action)
        if handler is None:
            return C.Result(False, "error", f"未知动作 {request.action}")

        with self.store.tx() as conn:
            cached = conn.execute(
                "SELECT result_json FROM idempotent_requests WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()
            if cached is not None:
                data = json.loads(cached["result_json"])
                return C.Result(data["accepted"], data["state"], data["message"], data.get("data", {}))

            actor = self.store.get(conn, "actors", actor_id, "actor_id")
            try:
                if actor is None:
                    raise C.AuthError("身份未登记")
                data = handler(conn, actor, request.payload, request.request_id)
                result = C.Result(True, data.get("state", "ok"), data.get("message", "已受理"), data)
            except C.DomainError as exc:
                result = C.Result(False, exc.code, str(exc))
            except (ValueError, TypeError) as exc:
                result = C.Result(False, "invalid", str(exc))
            except Exception as exc:  # 其余持久化约束冲突也按业务拒绝处理
                msg = str(exc)
                code = "conflict" if any(k in msg for k in
                                         ("UNIQUE", "CHECK", "FOREIGN KEY")) else "error"
                result = C.Result(False, code, f"请求被拒绝：{msg}")

            conn.execute(
                "INSERT INTO idempotent_requests (request_id, actor_id, action, result_json, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (request.request_id, actor_id, request.action,
                 json.dumps({"accepted": result.accepted, "state": result.state,
                             "message": result.message, "data": result.data}, ensure_ascii=False),
                 now()),
            )
            return result

    # ============================== 身份/主数据 ==============================
    def _require_role(self, actor: dict, *roles: str) -> None:
        if actor["role"] not in roles:
            raise C.AuthError(f"角色 {actor['role']} 无权执行该操作")

    def _require_project_scope(self, conn, actor: dict, project_id: str) -> dict:
        """返回项目行，并校验该角色有权操作该项目。"""
        project = self.store.get(conn, "projects", project_id)
        if project is None:
            raise C.NotFound("项目不存在")
        role = actor["role"]
        if role == C.ROLE_REGULATOR:
            return project
        if role == C.ROLE_DEVELOPER:
            if actor["scope_id"] != project["developer_id"]:
                raise C.AuthError("只能处理本公司项目")
            return project
        if role == C.ROLE_BANK:
            if actor["scope_id"] != project["bank_id"]:
                raise C.AuthError("只能处理本行监管账户")
            return project
        raise C.AuthError()

    def _register_developer(self, conn, actor, p, rid) -> dict:
        self._require_role(actor, C.ROLE_REGULATOR)
        self.store.insert(conn, "developers", {"id": p["id"], "name": p["name"]})
        self.store.audit(conn, actor["actor_id"], "register_developer", "developer", p["id"], rid)
        return {"message": f"项目公司 {p['name']} 已登记"}

    def _register_bank(self, conn, actor, p, rid) -> dict:
        self._require_role(actor, C.ROLE_REGULATOR)
        self.store.insert(conn, "banks", {"id": p["id"], "name": p["name"]})
        self.store.audit(conn, actor["actor_id"], "register_bank", "bank", p["id"], rid)
        return {"message": f"监管银行 {p['name']} 已登记"}

    def _register_actor(self, conn, actor, p, rid) -> dict:
        """监管人员登记系统账号。

        payload: id, role, display_name, scope_id（developer/bank 角色必填其机构 id；buyer 填购房人 id）
        """
        self._require_role(actor, C.ROLE_REGULATOR)
        role = p["role"]
        if role not in C.ROLES:
            raise ValueError(f"未知角色 {role}")
        scope = p.get("scope_id") or None
        if role in (C.ROLE_DEVELOPER, C.ROLE_BANK, C.ROLE_BUYER) and not scope:
            raise ValueError("该角色必须指定 scope_id")
        if role == C.ROLE_DEVELOPER and self.store.get(conn, "developers", scope) is None:
            raise ValueError("scope_id 不是已登记的项目公司")
        if role == C.ROLE_BANK and self.store.get(conn, "banks", scope) is None:
            raise ValueError("scope_id 不是已登记的银行")
        if role == C.ROLE_BUYER and self.store.get(conn, "buyers", scope) is None:
            raise ValueError("scope_id 不是已登记的购房人")
        self.store.insert(conn, "actors", {
            "actor_id": p["id"], "role": role,
            "display_name": p.get("display_name", ""), "scope_id": scope,
        })
        self.store.audit(conn, actor["actor_id"], "register_actor", "actor", p["id"], rid, {"role": role})
        return {"message": f"账号 {p['id']}（{role}）已登记"}

    def _issue_token(self, conn, actor, p, rid) -> dict:
        """为已登记账号签发 API token；明文仅本次返回，库内只存 SHA-256 散列。"""
        import hashlib
        import secrets
        self._require_role(actor, C.ROLE_REGULATOR)
        target = self.store.get(conn, "actors", p["actor_id"], "actor_id")
        if target is None:
            raise C.NotFound("账号不存在")
        plaintext = secrets.token_urlsafe(24)
        digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
        self.store.insert(conn, "api_tokens", {
            "token": digest, "actor_id": target["actor_id"],
            "issued_at": now(), "revoked": 0,
        })
        self.store.audit(conn, actor["actor_id"], "issue_token", "actor",
                         target["actor_id"], rid)
        return {"message": "token 已签发，仅显示一次", "actor_id": target["actor_id"],
                "token": plaintext}

    def _revoke_token(self, conn, actor, p, rid) -> dict:
        import hashlib
        self._require_role(actor, C.ROLE_REGULATOR)
        digest = hashlib.sha256(p["token"].encode("utf-8")).hexdigest()
        row = self.store.get(conn, "api_tokens", digest, "token")
        if row is None:
            raise C.NotFound("token 不存在")
        conn.execute("UPDATE api_tokens SET revoked = 1 WHERE token = ?", (digest,))
        self.store.audit(conn, actor["actor_id"], "revoke_token", "actor",
                         row["actor_id"], rid)
        return {"message": "token 已吊销", "actor_id": row["actor_id"]}

    def _create_project(self, conn, actor, p, rid) -> dict:
        self._require_role(actor, C.ROLE_REGULATOR)
        if self.store.get(conn, "developers", p["developer_id"]) is None:
            raise ValueError("项目公司未登记")
        if self.store.get(conn, "banks", p["bank_id"]) is None:
            raise ValueError("监管银行未登记")
        self.store.insert(conn, "projects", {
            "id": p["id"], "developer_id": p["developer_id"],
            "bank_id": p["bank_id"], "name": p["name"], "created_at": now(),
        })
        self.store.audit(conn, actor["actor_id"], "create_project", "project", p["id"], rid)
        return {"message": f"项目 {p['name']} 已创建", "project_id": p["id"]}

    def _open_account(self, conn, actor, p, rid) -> dict:
        project = self._require_project_scope(conn, actor, p["project_id"])
        self._require_role(actor, C.ROLE_REGULATOR, C.ROLE_BANK)
        if actor["role"] == C.ROLE_BANK and project["bank_id"] != actor["scope_id"]:
            raise C.AuthError("账户只能由项目监管行开立")
        if self.store.get(conn, "escrow_accounts", p["project_id"], "project_id"):
            raise C.ConflictError("该项目监管账户已存在")
        acct_id = p.get("id") or f"acct-{p['project_id']}"
        self.store.insert(conn, "escrow_accounts", {
            "id": acct_id, "project_id": p["project_id"],
            "account_no": p["account_no"], "bank_id": project["bank_id"],
            "currency": p.get("currency", "CNY"), "balance_cents": 0,
        })
        self.store.audit(conn, actor["actor_id"], "open_account", "account", acct_id, rid)
        return {"message": "监管账户已开立", "account_id": acct_id, "state": "open"}

    def _add_unit(self, conn, actor, p, rid) -> dict:
        project = self._require_project_scope(conn, actor, p["project_id"])
        self._require_role(actor, C.ROLE_REGULATOR, C.ROLE_DEVELOPER)
        price = to_cents(p["price"])
        self.store.insert(conn, "units", {
            "id": p["id"], "project_id": project["id"],
            "unit_no": p["unit_no"], "price_cents": price, "status": "available",
        })
        self.store.audit(conn, actor["actor_id"], "add_unit", "unit", p["id"], rid)
        return {"message": f"房源 {p['unit_no']} 已备案", "unit_id": p["id"],
                "price_yuan": from_cents(price)}

    def _register_buyer(self, conn, actor, p, rid) -> dict:
        self._require_role(actor, C.ROLE_REGULATOR, C.ROLE_DEVELOPER)
        self.store.insert(conn, "buyers", {
            "id": p["id"], "name": p["name"], "id_no": p["id_no"],
            "bank_account": p.get("bank_account", ""),
        })
        self.store.audit(conn, actor["actor_id"], "register_buyer", "buyer", p["id"], rid)
        return {"message": f"购房人 {p['name']} 已登记"}

    # ============================== 合同 + 收款计划 ==============================
    def _sign_contract(self, conn, actor, p, rid) -> dict:
        """项目公司网签。payload: id, contract_no, project_id, unit_id, buyer_id, deposit, total_price?"""
        project = self._require_project_scope(conn, actor, p["project_id"])
        self._require_role(actor, C.ROLE_REGULATOR, C.ROLE_DEVELOPER)

        unit = self.store.get(conn, "units", p["unit_id"])
        if unit is None or unit["project_id"] != project["id"]:
            raise C.NotFound("房源不存在于该项目")
        buyer = self.store.get(conn, "buyers", p["buyer_id"])
        if buyer is None:
            raise C.NotFound("购房人未登记")

        # 先校验合同条款本身（与房源当前状态无关）
        total = to_cents(p.get("total_price") or unit["price_cents"] / 100)
        if total <= 0:
            raise ValueError("合同总价必须为正")
        deposit = to_cents(p["deposit"])
        if deposit <= 0:
            raise ValueError("定金金额必须为正")
        # 定金不得超过总价的 3%（整数分比较，避免浮点误差）
        if deposit * 100 > total * int(C.DEPOSIT_RATIO_CAP * 100):
            raise ValueError(
                f"定金 {from_cents(deposit)} 元超过总价 3% 上限"
                f"（{from_cents(total * 3 // 100)} 元）")
        if deposit > total:
            raise ValueError("定金不能超过总价")

        # 条款合法后再检查房源可售状态
        if unit["status"] != "available":
            raise C.ConflictError("房源已签约或不可售")

        cid = p["id"]
        self.store.insert(conn, "contracts", {
            "id": cid, "contract_no": p["contract_no"], "project_id": project["id"],
            "unit_id": unit["id"], "buyer_id": buyer["id"],
            "total_price_cents": total, "deposit_cents": deposit,
            "status": C.CONTRACT_DRAFT, "created_at": now(),
        })
        # 收款计划：seq=1 定金，seq=2 尾款；定金+尾款=总价
        rest = total - deposit
        self.store.insert(conn, "payment_schedule", {
            "id": f"{cid}-s1", "contract_id": cid, "seq": 1,
            "item_type": C.SCHEDULE_DEPOSIT, "amount_cents": deposit,
            "due_date": p.get("deposit_due", ""), "status": C.ITEM_PENDING, "paid_at": None,
        })
        if rest > 0:
            self.store.insert(conn, "payment_schedule", {
                "id": f"{cid}-s2", "contract_id": cid, "seq": 2,
                "item_type": C.SCHEDULE_INSTALLMENT, "amount_cents": rest,
                "due_date": p.get("balance_due", ""), "status": C.ITEM_PENDING, "paid_at": None,
            })
        conn.execute("UPDATE units SET status = 'contracted' WHERE id = ?", (unit["id"],))
        self.store.audit(conn, actor["actor_id"], "sign_contract", "contract", cid, rid,
                         {"total": total, "deposit": deposit})
        return {
            "message": "合同已网签，待定金到账进入监管", "contract_id": cid,
            "state": C.CONTRACT_DRAFT,
            "total_price_yuan": from_cents(total),
            "deposit_yuan": from_cents(deposit),
            "deposit_cap_yuan": f"{total * C.DEPOSIT_RATIO_CAP / 100:.2f}",
        }

    # ============================== 银行回调（入账 / 出账） ==============================
    def _bank_callback(self, conn, actor, p, rid) -> dict:
        """监管银行推送资金处理结果。

        入账回调：payload callback_id, direction='in', project_id, contract_id, seq,
                  amount, bank_ref  —— 购房人款进入监管账户。
        出账回调：payload callback_id, instr_id, status(settled|failed), bank_ref, failure_code
        """
        self._require_role(actor, C.ROLE_BANK)
        cb_id = p["callback_id"]
        if conn.execute("SELECT 1 FROM bank_callbacks WHERE callback_id = ?", (cb_id,)).fetchone():
            # 重送：原样返回既有结果，绝不重复划款
            old = conn.execute(
                "SELECT i.id, i.status FROM bank_callbacks b JOIN fund_instructions i ON i.id = b.instr_id"
                " WHERE b.callback_id = ?", (cb_id,)).fetchone()
            return {"message": "回调重复，已忽略", "callback_id": cb_id,
                    "instr_id": old["id"], "state": old["status"], "deduplicated": True}

        if p.get("direction") == C.FLOW_IN:
            return self._settle_inflow(conn, actor, p, rid, cb_id)
        return self._settle_outflow(conn, actor, p, rid, cb_id)

    def _account_of_project(self, conn, project_id: str) -> dict:
        acct = self.store.get(conn, "escrow_accounts", project_id, "project_id")
        if acct is None:
            raise C.ConflictError("项目监管账户尚未开立")
        return acct

    def _settle_inflow(self, conn, actor, p, rid, cb_id) -> dict:
        project = self._require_project_scope(conn, actor, p["project_id"])
        contract = self.store.get(conn, "contracts", p["contract_id"])
        if contract is None or contract["project_id"] != project["id"]:
            raise C.NotFound("合同不存在于该项目")
        if contract["status"] in C.TERMINAL_STATES:
            raise C.ConflictError("合同已终止，不能再收款")
        item = conn.execute(
            "SELECT * FROM payment_schedule WHERE contract_id = ? AND seq = ?",
            (contract["id"], p["seq"])).fetchone()
        if item is None:
            raise C.NotFound("收款计划条目不存在")
        if item["status"] == C.ITEM_PAID:
            # 同一期款项已到账：可能是银行换了回调编号重送，按指令幂等返回，绝不重复入账
            old = self.store.get(conn, "fund_instructions",
                                 f"in:{contract['id']}:{item['seq']}", "idem_key")
            return {"message": "该期款项已入账，重复回调忽略", "deduplicated": True,
                    "instr_id": old["id"] if old else None, "state": C.INSTR_SETTLED}
        if item["item_type"] == C.SCHEDULE_INSTALLMENT and contract["status"] == C.CONTRACT_DRAFT:
            raise C.ConflictError("定金到账合同生效前，不能收取尾款")
        amount = to_cents(p["amount"])
        if amount != item["amount_cents"]:
            raise ValueError(f"到账金额 {from_cents(amount)} 与应缴 {from_cents(item['amount_cents'])} 不一致")

        acct = self._account_of_project(conn, project["id"])
        # 入账指令也走指令表并以 idem_key 去重（银行可能先建指令后回调）
        idem = f"in:{contract['id']}:{item['seq']}"
        instr = self.store.get(conn, "fund_instructions", idem, "idem_key")
        if instr is None:
            instr_id = f"in-{contract['id']}-s{item['seq']}"
            self.store.insert(conn, "fund_instructions", {
                "id": instr_id, "project_id": project["id"], "contract_id": contract["id"],
                "flow_type": C.FLOW_IN, "amount_cents": amount, "destination": "",
                "reason": f"{item['item_type']} 入账", "status": C.INSTR_SETTLED,
                "idem_key": idem, "bank_ref": p.get("bank_ref", ""),
                "failure_code": "", "created_at": now(), "settled_at": now(),
            })
        else:
            instr_id = instr["id"]
            conn.execute("UPDATE fund_instructions SET status = ?, bank_ref = ?, settled_at = ? WHERE id = ?",
                         (C.INSTR_SETTLED, p.get("bank_ref", ""), now(), instr_id))

        balance = acct["balance_cents"] + amount
        conn.execute("UPDATE escrow_accounts SET balance_cents = ? WHERE id = ?", (balance, acct["id"]))
        conn.execute(
            "INSERT INTO ledger_entries (account_id, contract_id, direction, amount_cents,"
            " balance_after_cents, instr_id, summary, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (acct["id"], contract["id"], "in", amount, balance, instr_id,
             f"{item['item_type']} 入账", now()))
        conn.execute(
            "UPDATE payment_schedule SET status = ?, paid_at = ? WHERE id = ?",
            (C.ITEM_PAID, now(), item["id"]))

        # 款项状态推进合同：定金到账 -> active；全部款项到账 -> 仍为 active（等待节点）
        new_status = contract["status"]
        if item["item_type"] == C.SCHEDULE_DEPOSIT and contract["status"] == C.CONTRACT_DRAFT:
            new_status = C.CONTRACT_ACTIVE
            conn.execute("UPDATE contracts SET status = ? WHERE id = ?", (new_status, contract["id"]))

        conn.execute(
            "INSERT INTO bank_callbacks (callback_id, instr_id, status, payload, received_at)"
            " VALUES (?,?,?,?,?)",
            (cb_id, instr_id, C.INSTR_SETTLED, json.dumps(p, ensure_ascii=False), now()))
        self.store.audit(conn, actor["actor_id"], "bank_callback_in", "instruction", instr_id, rid,
                         {"amount": amount, "balance": balance})
        return {"message": "入账成功", "instr_id": instr_id, "state": C.INSTR_SETTLED,
                "balance_yuan": from_cents(balance), "contract_status": new_status}

    def _settle_outflow(self, conn, actor, p, rid, cb_id) -> dict:
        instr = self.store.get(conn, "fund_instructions", p["instr_id"])
        if instr is None:
            raise C.NotFound("出金指令不存在")
        # 只能由项目监管行处理
        project = self.store.get(conn, "projects", instr["project_id"])
        if project["bank_id"] != actor["scope_id"]:
            raise C.AuthError("只能处理本行监管项目的指令")
        if instr["status"] == C.INSTR_SETTLED:
            # 指令已结算且回调重送：幂等返回，绝不再划款
            conn.execute(
                "INSERT INTO bank_callbacks (callback_id, instr_id, status, payload, received_at)"
                " VALUES (?,?,?,?,?)",
                (cb_id, instr["id"], "duplicate", json.dumps(p, ensure_ascii=False), now()))
            return {"message": "指令已结算，重复回调忽略", "instr_id": instr["id"],
                    "state": C.INSTR_SETTLED, "deduplicated": True}
        if instr["status"] in (C.INSTR_FAILED, C.INSTR_CANCELLED):
            raise C.ConflictError("指令已失效，不能结算（如需划款请重新发起）")

        status = p.get("status")
        if status == C.INSTR_FAILED:
            conn.execute("UPDATE fund_instructions SET status = ?, failure_code = ? WHERE id = ?",
                         (C.INSTR_FAILED, p.get("failure_code", ""), instr["id"]))
            conn.execute(
                "INSERT INTO bank_callbacks (callback_id, instr_id, status, payload, received_at)"
                " VALUES (?,?,?,?,?)",
                (cb_id, instr["id"], C.INSTR_FAILED, json.dumps(p, ensure_ascii=False), now()))
            self.store.audit(conn, actor["actor_id"], "bank_callback_fail", "instruction", instr["id"], rid)
            return {"message": "出金失败已登记", "instr_id": instr["id"], "state": C.INSTR_FAILED}

        if status != C.INSTR_SETTLED:
            raise ValueError("回调 status 只能是 settled/failed")

        # 项目公司违约金从其自有账户支付，不动监管账户余额
        if instr["flow_type"] == C.PENALTY_DEVELOPER:
            conn.execute(
                "UPDATE fund_instructions SET status = ?, bank_ref = ?, settled_at = ? WHERE id = ?",
                (C.INSTR_SETTLED, p.get("bank_ref", ""), now(), instr["id"]))
            conn.execute(
                "INSERT INTO bank_callbacks (callback_id, instr_id, status, payload, received_at)"
                " VALUES (?,?,?,?,?)",
                (cb_id, instr["id"], C.INSTR_SETTLED, json.dumps(p, ensure_ascii=False), now()))
            self.store.audit(conn, actor["actor_id"], "bank_callback_penalty",
                             "instruction", instr["id"], rid, {"amount": amount})
            return {"message": "项目公司违约金已划付购房人", "instr_id": instr["id"],
                    "state": C.INSTR_SETTLED}

        acct = self._account_of_project(conn, instr["project_id"])
        amount = instr["amount_cents"]
        if acct["balance_cents"] < amount:
            raise C.ConflictError("监管账户余额不足，无法划款")
        balance = acct["balance_cents"] - amount
        conn.execute("UPDATE escrow_accounts SET balance_cents = ? WHERE id = ?", (balance, acct["id"]))
        conn.execute(
            "INSERT INTO ledger_entries (account_id, contract_id, direction, amount_cents,"
            " balance_after_cents, instr_id, summary, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (acct["id"], instr["contract_id"], "out", amount, balance, instr["id"],
             instr["reason"], now()))
        conn.execute(
            "UPDATE fund_instructions SET status = ?, bank_ref = ?, settled_at = ? WHERE id = ?",
            (C.INSTR_SETTLED, p.get("bank_ref", ""), now(), instr["id"]))
        conn.execute(
            "INSERT INTO bank_callbacks (callback_id, instr_id, status, payload, received_at)"
            " VALUES (?,?,?,?,?)",
            (cb_id, instr["id"], C.INSTR_SETTLED, json.dumps(p, ensure_ascii=False), now()))
        self.store.audit(conn, actor["actor_id"], "bank_callback_out", "instruction", instr["id"], rid,
                         {"amount": amount, "balance": balance})

        # 出金结算后驱动最终状态
        self._apply_outflow_effect(conn, instr, balance)
        contract = self.store.get(conn, "contracts", instr["contract_id"]) if instr["contract_id"] else None
        return {"message": "出金成功", "instr_id": instr["id"], "state": C.INSTR_SETTLED,
                "balance_yuan": from_cents(balance),
                "contract_status": contract["status"] if contract else None}

    # ============================== 节点事件 ==============================
    def _record_event(self, conn, actor, p, rid) -> dict:
        """登记/确认节点与异常事件，生成确定性的资金指令。

        payload: contract_id, event_type, detail?
        - structure_top：监管确认，合同 active -> topped_out，按累计 60% 解冻给项目公司
        - acceptance：监管确认，topped_out -> accepted，解冻剩余监管款（购房人已缴部分）
        - overdue：监管确认逾期未验收 -> 解冻停止，已收款全额退购房人，合同关闭
        - developer_breach / cancel_developer：全额退购房人（含项目公司违约金，如有）
        - buyer_breach / cancel_buyer：定金罚没给项目公司，其余退购房人
        """
        contract = self.store.get(conn, "contracts", p["contract_id"])
        if contract is None:
            raise C.NotFound("合同不存在")
        project = self._require_project_scope(conn, actor, contract["project_id"])
        event_type = p["event_type"]
        if event_type not in (C.NODE_EVENTS + (C.EVENT_OVERDUE, C.EVENT_DEVELOPER_BREACH,
                                               C.EVENT_BUYER_BREACH, C.EVENT_CANCEL_DEVELOPER,
                                               C.EVENT_CANCEL_BUYER)):
            raise ValueError("未知事件类型")

        # 谁能登记：封顶/验收/逾期/项目公司违约由监管确认；取消/购房人违约按发起方
        if event_type in (C.EVENT_STRUCTURE_TOP, C.EVENT_ACCEPTANCE, C.EVENT_OVERDUE,
                          C.EVENT_DEVELOPER_BREACH):
            self._require_role(actor, C.ROLE_REGULATOR)
        elif event_type in (C.EVENT_CANCEL_DEVELOPER,):
            self._require_role(actor, C.ROLE_REGULATOR, C.ROLE_DEVELOPER)
        else:  # buyer_breach / cancel_buyer
            self._require_role(actor, C.ROLE_REGULATOR, C.ROLE_DEVELOPER)

        status = contract["status"]
        if status in C.TERMINAL_STATES:
            raise C.ConflictError(f"合同已终态（{status}），不能再登记事件")

        seq_row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM contract_events WHERE contract_id = ?",
                               (contract["id"],)).fetchone()
        seq = seq_row["n"]

        instructions: list[dict] = []

        if event_type == C.EVENT_STRUCTURE_TOP:
            if status != C.CONTRACT_ACTIVE:
                raise C.ConflictError("主体封顶只能在合同生效（定金到账）后确认")
            conn.execute("UPDATE contracts SET status = ? WHERE id = ?",
                         (C.CONTRACT_STRUCTURE_TOPPED_OUT, contract["id"]))
            new_status = C.CONTRACT_STRUCTURE_TOPPED_OUT
            instructions.append(self._release_to_developer(
                conn, project, contract, C.EVENT_STRUCTURE_TOP,
                C.RELEASE_RATIO[C.EVENT_STRUCTURE_TOP],
                f"主体封顶节点解冻（累计 {int(C.RELEASE_RATIO[C.EVENT_STRUCTURE_TOP] * 100)}%）"))

        elif event_type == C.EVENT_ACCEPTANCE:
            if status != C.CONTRACT_STRUCTURE_TOPPED_OUT:
                raise C.ConflictError("必须先确认主体封顶，才能验收")
            conn.execute("UPDATE contracts SET status = ? WHERE id = ?",
                         (C.CONTRACT_ACCEPTED, contract["id"]))
            new_status = C.CONTRACT_ACCEPTED
            instructions.append(self._release_to_developer(
                conn, project, contract, C.EVENT_ACCEPTANCE,
                C.RELEASE_RATIO[C.EVENT_ACCEPTANCE],
                "竣工验收节点解冻（累计 100%）"))

        elif event_type == C.EVENT_OVERDUE:
            if status not in (C.CONTRACT_ACTIVE, C.CONTRACT_STRUCTURE_TOPPED_OUT):
                raise C.ConflictError("当前合同状态不允许逾期关闭")
            conn.execute("UPDATE contracts SET status = ? WHERE id = ?",
                         (C.CONTRACT_OVERDUE_CLOSED, contract["id"]))
            new_status = C.CONTRACT_OVERDUE_CLOSED
            instructions += self._terminate_refund(conn, project, contract,
                                                   "逾期未验收，监管账户内购房人资金全额退还", 0)

        elif event_type in (C.EVENT_DEVELOPER_BREACH, C.EVENT_CANCEL_DEVELOPER):
            if status not in (C.CONTRACT_ACTIVE, C.CONTRACT_STRUCTURE_TOPPED_OUT, C.CONTRACT_ACCEPTED):
                raise C.ConflictError("当前合同状态不允许该事件")
            conn.execute("UPDATE contracts SET status = ? WHERE id = ?",
                         (C.CONTRACT_CANCELLED_DEVELOPER, contract["id"]))
            new_status = C.CONTRACT_CANCELLED_DEVELOPER
            penalty = self._penalty_amount(contract, p, event_type)
            reason = ("项目公司违约，监管账户内购房人资金全额退还，另由项目公司支付违约金"
                      if event_type == C.EVENT_DEVELOPER_BREACH
                      else "项目公司取消，监管账户内购房人资金全额退还")
            instructions += self._terminate_refund(conn, project, contract, reason, penalty)

        else:  # buyer_breach / cancel_buyer
            if status not in (C.CONTRACT_DRAFT, C.CONTRACT_ACTIVE, C.CONTRACT_STRUCTURE_TOPPED_OUT):
                raise C.ConflictError("验收后购房人不再承担取消责任")
            conn.execute("UPDATE contracts SET status = ? WHERE id = ?",
                         (C.CONTRACT_CANCELLED_BUYER, contract["id"]))
            new_status = C.CONTRACT_CANCELLED_BUYER
            instructions += self._terminate_buyer_fault(conn, project, contract, p)

        eid = p.get("event_id") or f"evt-{contract['id']}-{seq}"
        self.store.insert(conn, "contract_events", {
            "id": eid, "contract_id": contract["id"], "event_type": event_type, "seq": seq,
            "detail": json.dumps(p.get("detail", {}), ensure_ascii=False),
            "confirmed_by": actor["actor_id"], "confirmed_at": now(),
        })
        conn.execute("UPDATE units SET status = 'available' WHERE id = ?", (contract["unit_id"],))
        self.store.audit(conn, actor["actor_id"], "record_event", "event", eid, rid,
                         {"contract": contract["id"], "event": event_type,
                          "instructions": [i["id"] for i in instructions]})
        return {
            "message": f"事件 {event_type} 已确认", "event_id": eid,
            "state": new_status,
            "contract_status": new_status,
            "instructions": [{"id": i["id"], "flow_type": i["flow_type"],
                              "amount_yuan": from_cents(i["amount_cents"]),
                              "destination": i["destination"], "status": i["status"]}
                             for i in instructions],
        }

    # ---- 出金指令构造 ----
    def _settled_inflow_total(self, conn, contract_id: str) -> int:
        return conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM fund_instructions"
            " WHERE contract_id = ? AND flow_type = ? AND status = ?",
            (contract_id, C.FLOW_IN, C.INSTR_SETTLED)).fetchone()["s"]

    def _settled_escrow_out_total(self, conn, contract_id: str) -> int:
        marks = ",".join("?" for _ in C.ESCROW_DRAWING_FLOWS)
        return conn.execute(
            f"SELECT COALESCE(SUM(amount_cents),0) AS s FROM fund_instructions"
            f" WHERE contract_id = ? AND flow_type IN ({marks}) AND status = ?",
            (contract_id, *C.ESCROW_DRAWING_FLOWS, C.INSTR_SETTLED)).fetchone()["s"]

    def _retained(self, conn, contract_id: str) -> int:
        """监管账户内仍属于该合同的留存资金 = 已入账 - 已划出监管账户。"""
        return (self._settled_inflow_total(conn, contract_id)
                - self._settled_escrow_out_total(conn, contract_id))

    def _cancel_pending_draws(self, conn, contract_id: str) -> None:
        """终止事件发生时，撤销该合同所有尚未结算的动用监管资金指令，阻止提前/事后放款。"""
        marks = ",".join("?" for _ in C.ESCROW_DRAWING_FLOWS)
        conn.execute(
            f"UPDATE fund_instructions SET status = '{C.INSTR_CANCELLED}'"
            f" WHERE contract_id = ? AND flow_type IN ({marks}) AND status = ?",
            (contract_id, *C.ESCROW_DRAWING_FLOWS, C.INSTR_PENDING))

    def _release_to_developer(self, conn, project, contract, node_event: str,
                              cumulative_ratio: float, reason: str) -> dict:
        """按累计解冻比例计算本节点应拨：已缴×累计比例 − 已（拟）拨付，且不超过监管留存。"""
        paid_total = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM payment_schedule"
            " WHERE contract_id = ? AND status = ?",
            (contract["id"], C.ITEM_PAID)).fetchone()["s"]
        already = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM fund_instructions"
            " WHERE contract_id = ? AND flow_type = ? AND status IN (?, ?)",
            (contract["id"], C.FLOW_OUT_DEVELOPER,
             C.INSTR_PENDING, C.INSTR_SETTLED)).fetchone()["s"]
        target = int(paid_total * cumulative_ratio)  # 累计应解冻
        amount = max(0, min(target - already, self._retained(conn, contract["id"])))
        return self._create_out_instr(
            conn, project, contract, C.FLOW_OUT_DEVELOPER, amount,
            C.DEST_DEVELOPER, reason, idem=f"rel:{contract['id']}:{node_event}")

    def _mark_schedule_terminal(self, conn, contract_id: str, forfeit: int) -> None:
        """按资金事实消化收款计划条目：先冲已拨付项目公司，再扣罚没，其余为退款。"""
        disbursed = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM fund_instructions"
            " WHERE contract_id = ? AND flow_type IN (?, ?) AND status = ?",
            (contract_id, C.FLOW_OUT_DEVELOPER, C.PENALTY_BUYER,
             C.INSTR_SETTLED)).fetchone()["s"]
        rows = conn.execute(
            "SELECT id, amount_cents FROM payment_schedule"
            " WHERE contract_id = ? AND status = ? ORDER BY seq",
            (contract_id, C.ITEM_PAID)).fetchall()
        for row in rows:
            amt = row["amount_cents"]
            d = min(amt, disbursed)
            disbursed -= d
            f = min(amt - d, forfeit)
            forfeit -= f
            if d == amt:
                status = C.ITEM_DISBURSED
            elif d > 0:
                status = C.ITEM_PARTIAL_DISBURSED
            elif f > 0:
                status = C.ITEM_FORFEITED
            else:
                status = C.ITEM_REFUNDED
            conn.execute("UPDATE payment_schedule SET status = ? WHERE id = ?", (status, row["id"]))

    def _terminate_refund(self, conn, project, contract, reason: str,
                          developer_penalty: int = 0) -> list[dict]:
        """非购房人过错终止（逾期/项目公司违约或取消）：

        - 撤销尚未拨付的节点解冻指令，防止事后放款；
        - 监管账户中该合同的留存资金全额退购房人；
        - 项目公司违约金从其自有资金划付购房人，不动监管余额。
        """
        out: list[dict] = []
        self._cancel_pending_draws(conn, contract["id"])
        retained = self._retained(conn, contract["id"])
        if retained > 0:
            out.append(self._create_out_instr(
                conn, project, contract, C.FLOW_OUT_BUYER, retained,
                C.DEST_BUYER, reason, idem=f"ref:{contract['id']}"))
        self._mark_schedule_terminal(conn, contract["id"], forfeit=0)
        if developer_penalty > 0:
            out.append(self._create_out_instr(
                conn, project, contract, C.PENALTY_DEVELOPER, developer_penalty,
                C.DEST_BUYER, "项目公司违约金（自有资金划付购房人）",
                idem=f"pen:{contract['id']}:dev"))
        return out

    def _terminate_buyer_fault(self, conn, project, contract, p) -> list[dict]:
        """购房人违约/取消：定金（违约金）归项目公司，其余留存款退购房人。

        以监管账户内仍留存的资金为上限：已按节点拨付给项目公司的部分不追回，
        但其中的定金性质部分按违约约定处理——本实现约定：违约金只从监管留存中扣划，
        金额取 detail.penalty 与已付定金、监管留存三者的最小值。
        """
        out: list[dict] = []
        self._cancel_pending_draws(conn, contract["id"])
        retained = self._retained(conn, contract["id"])
        deposit_paid = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM payment_schedule"
            " WHERE contract_id = ? AND item_type = ? AND status = ?",
            (contract["id"], C.SCHEDULE_DEPOSIT, C.ITEM_PAID)).fetchone()["s"]
        detail = p.get("detail") if isinstance(p.get("detail"), dict) else {}
        raw_penalty = detail.get("penalty", deposit_paid / 100)
        penalty = min(to_cents(raw_penalty), deposit_paid, retained)
        if penalty > 0:
            out.append(self._create_out_instr(
                conn, project, contract, C.PENALTY_BUYER, penalty,
                C.DEST_DEVELOPER, "购房人违约/取消，定金违约金划付项目公司",
                idem=f"pen:{contract['id']}:buyer"))
        refund = retained - penalty
        if refund > 0:
            out.append(self._create_out_instr(
                conn, project, contract, C.FLOW_OUT_BUYER, refund,
                C.DEST_BUYER, "购房人违约/取消，扣除违约金后余款退还",
                idem=f"ref:{contract['id']}"))
        self._mark_schedule_terminal(conn, contract["id"], forfeit=penalty)
        return out

    @staticmethod
    def _penalty_amount(contract, p, event_type) -> int:
        if event_type != C.EVENT_DEVELOPER_BREACH:
            return 0
        raw = p.get("detail", {}).get("penalty", 0) if isinstance(p.get("detail"), dict) else 0
        return to_cents(raw)

    def _create_out_instr(self, conn, project, contract, flow_type, amount,
                          destination, reason, idem) -> dict:
        existing = self.store.get(conn, "fund_instructions", idem, "idem_key")
        if existing is not None:
            return existing
        if amount <= 0:
            # 零金额不产生实际划款指令，返回虚拟描述，避免无意义指令
            return {"id": None, "flow_type": flow_type, "amount_cents": 0,
                    "destination": destination, "status": "noop"}
        instr_id = f"out-{uuid.uuid4().hex[:12]}"
        row = {
            "id": instr_id, "project_id": project["id"], "contract_id": contract["id"],
            "flow_type": flow_type, "amount_cents": amount, "destination": destination,
            "reason": reason, "status": C.INSTR_PENDING, "idem_key": idem,
            "bank_ref": "", "failure_code": "", "created_at": now(), "settled_at": None,
        }
        self.store.insert(conn, "fund_instructions", row)
        return row

    def _apply_outflow_effect(self, conn, instr: dict, balance: int) -> None:
        """出金结算后的收尾：验收且款全拨完则合同完全履行。"""
        if instr["flow_type"] != C.FLOW_OUT_DEVELOPER:
            return
        cid = instr["contract_id"]
        contract = self.store.get(conn, "contracts", cid)
        if contract is None or contract["status"] != C.CONTRACT_ACCEPTED:
            return
        paid_total = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM payment_schedule"
            " WHERE contract_id = ? AND status = ?", (cid, C.ITEM_PAID)).fetchone()["s"]
        released = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM fund_instructions"
            " WHERE contract_id = ? AND flow_type = ? AND status = ?",
            (cid, C.FLOW_OUT_DEVELOPER, C.INSTR_SETTLED)).fetchone()["s"]
        if paid_total > 0 and released >= paid_total:
            conn.execute("UPDATE contracts SET status = ? WHERE id = ?",
                         (C.CONTRACT_COMPLETED, cid))
            conn.execute(
                "UPDATE payment_schedule SET status = ? WHERE contract_id = ? AND status = ?",
                (C.ITEM_DISBURSED, cid, C.ITEM_PAID))
            conn.execute("UPDATE units SET status = 'sold' WHERE id = ?", (contract["unit_id"],))

    # ============================== 查询 ==============================
    def _visible_contract(self, conn, actor: dict, contract_id: str) -> dict:
        contract = self.store.get(conn, "contracts", contract_id)
        if contract is None:
            raise C.NotFound("合同不存在")
        role = actor["role"]
        if role == C.ROLE_REGULATOR:
            return contract
        project = self.store.get(conn, "projects", contract["project_id"])
        if role == C.ROLE_DEVELOPER and actor["scope_id"] == project["developer_id"]:
            return contract
        if role == C.ROLE_BANK and actor["scope_id"] == project["bank_id"]:
            return contract
        if role == C.ROLE_BUYER:
            buyer_id = actor["scope_id"]
            if contract["buyer_id"] == buyer_id:
                return contract
        raise C.AuthError()

    def _get_contract(self, conn, actor, p, rid) -> dict:
        contract = self._visible_contract(conn, actor, p["contract_id"])
        project = self.store.get(conn, "projects", contract["project_id"])
        unit = self.store.get(conn, "units", contract["unit_id"])
        buyer = self.store.get(conn, "buyers", contract["buyer_id"])
        schedule = [dict(r) for r in conn.execute(
            "SELECT seq, item_type, amount_cents, status, paid_at, due_date"
            " FROM payment_schedule WHERE contract_id = ? ORDER BY seq", (contract["id"],))]
        events = [dict(r) for r in conn.execute(
            "SELECT event_type, seq, confirmed_by, confirmed_at, detail"
            " FROM contract_events WHERE contract_id = ? ORDER BY seq", (contract["id"],))]
        return {
            "state": contract["status"],
            "contract": {
                "id": contract["id"], "contract_no": contract["contract_no"],
                "project_id": contract["project_id"], "project_name": project["name"],
                "unit_no": unit["unit_no"], "buyer_name": buyer["name"],
                "total_price_yuan": from_cents(contract["total_price_cents"]),
                "deposit_yuan": from_cents(contract["deposit_cents"]),
                "status": contract["status"], "created_at": contract["created_at"],
            },
            "schedule": [{"seq": s["seq"], "type": s["item_type"],
                          "amount_yuan": from_cents(s["amount_cents"]),
                          "status": s["status"], "due_date": s["due_date"],
                          "paid_at": s["paid_at"]} for s in schedule],
            "events": events,
        }

    def _get_project(self, conn, actor, p, rid) -> dict:
        project = self._require_project_scope(conn, actor, p["project_id"])
        units = [dict(r) for r in conn.execute(
            "SELECT id, unit_no, price_cents, status FROM units WHERE project_id = ? ORDER BY unit_no",
            (project["id"],))]
        return {"state": "ok", "project": {
            "id": project["id"], "name": project["name"],
            "developer_id": project["developer_id"], "bank_id": project["bank_id"],
            "units": [{"id": u["id"], "unit_no": u["unit_no"],
                       "price_yuan": from_cents(u["price_cents"]), "status": u["status"]}
                      for u in units]}}

    def _get_account(self, conn, actor, p, rid) -> dict:
        project = self._require_project_scope(conn, actor, p["project_id"])
        acct = self._account_of_project(conn, project["id"])
        # 购房人不能直接查整个监管账户
        self._require_role(actor, C.ROLE_REGULATOR, C.ROLE_DEVELOPER, C.ROLE_BANK)
        return {"state": "ok", "account": {
            "id": acct["id"], "account_no": acct["account_no"],
            "project_id": project["id"], "currency": acct["currency"],
            "balance_yuan": from_cents(acct["balance_cents"])}}

    def _list_instructions(self, conn, actor, p, rid) -> dict:
        """列出待处理/全部指令。银行看本行，项目公司看本项目，监管看全部，购房人仅本人合同。"""
        sql = ("SELECT i.* FROM fund_instructions i")
        args: list[Any] = []
        where: list[str] = []
        if actor["role"] == C.ROLE_BANK:
            sql += " JOIN projects pr ON pr.id = i.project_id"
            where.append("pr.bank_id = ?")
            args.append(actor["scope_id"])
        elif actor["role"] == C.ROLE_DEVELOPER:
            where.append("i.project_id IN (SELECT id FROM projects WHERE developer_id = ?)")
            args.append(actor["scope_id"])
        elif actor["role"] == C.ROLE_BUYER:
            where.append("i.contract_id IN (SELECT id FROM contracts WHERE buyer_id = ?)")
            args.append(actor["scope_id"])
        elif actor["role"] != C.ROLE_REGULATOR:
            raise C.AuthError()
        if p.get("project_id"):
            # 非监管角色额外限定具体项目时再校验范围
            self._require_project_scope(conn, actor, p["project_id"])
            where.append("i.project_id = ?")
            args.append(p["project_id"])
        if p.get("status"):
            where.append("i.status = ?")
            args.append(p["status"])
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY i.created_at"
        rows = conn.execute(sql, args).fetchall()
        return {"state": "ok", "instructions": [{
            "id": r["id"], "project_id": r["project_id"], "contract_id": r["contract_id"],
            "flow_type": r["flow_type"], "amount_yuan": from_cents(r["amount_cents"]),
            "destination": r["destination"], "reason": r["reason"],
            "status": r["status"], "bank_ref": r["bank_ref"],
            "created_at": r["created_at"], "settled_at": r["settled_at"],
        } for r in rows]}

    def _fund_trail(self, conn, actor, p, rid) -> dict:
        """还原购房人资金从进入监管账户到最终去向的完整依据（证据链）。

        输出：合同信息、每笔入账（银行回单 callback_id/bank_ref）、
        每笔出账（事件依据 event_id/confirmed_by、指令、银行回单）、
        分类账序列、以及勾稽校验（已入账 = 已出账 + 账户中仍留存的本合同金额）。
        """
        contract = self._visible_contract(conn, actor, p["contract_id"])
        inflows = conn.execute(
            "SELECT i.*, b.callback_id AS cb, b.received_at AS cb_at"
            " FROM fund_instructions i LEFT JOIN bank_callbacks b ON b.instr_id = i.id AND b.status = 'settled'"
            " WHERE i.contract_id = ? AND i.flow_type = ? ORDER BY i.created_at",
            (contract["id"], C.FLOW_IN)).fetchall()
        outflows = conn.execute(
            "SELECT i.*, b.callback_id AS cb, b.received_at AS cb_at"
            " FROM fund_instructions i"
            " LEFT JOIN bank_callbacks b ON b.instr_id = i.id AND b.status = 'settled'"
            " WHERE i.contract_id = ? AND i.flow_type != ? ORDER BY i.created_at",
            (contract["id"], C.FLOW_IN)).fetchall()
        # 事件依据通过指令 reason/idempotent key 关联（idem 中带合同+事件），直接查合同事件
        events = {e["event_type"]: dict(e) for e in conn.execute(
            "SELECT * FROM contract_events WHERE contract_id = ?", (contract["id"],)).fetchall()}
        ledger = conn.execute(
            "SELECT * FROM ledger_entries WHERE contract_id = ? ORDER BY id",
            (contract["id"],)).fetchall()
        # 合同维度的留存余额（入 + / 出 −），不暴露整个监管账户的汇总余额
        ledger_out = []
        running = 0
        for r in ledger:
            running += r["amount_cents"] if r["direction"] == "in" else -r["amount_cents"]
            ledger_out.append({"seq": r["id"], "direction": r["direction"],
                               "amount_yuan": from_cents(r["amount_cents"]),
                               "contract_retained_after_yuan": from_cents(running),
                               "account_balance_after_yuan":
                                   from_cents(r["balance_after_cents"])
                                   if actor["role"] in (C.ROLE_REGULATOR, C.ROLE_BANK,
                                                        C.ROLE_DEVELOPER) else None,
                               "summary": r["summary"], "created_at": r["created_at"]})

        total_in = sum(r["amount_cents"] for r in inflows if r["status"] == C.INSTR_SETTLED)
        escrow_out = [r for r in outflows
                      if r["status"] == C.INSTR_SETTLED and r["flow_type"] in C.ESCROW_DRAWING_FLOWS]
        total_out = sum(r["amount_cents"] for r in escrow_out)
        dev_penalty_out = sum(r["amount_cents"] for r in outflows
                              if r["status"] == C.INSTR_SETTLED
                              and r["flow_type"] == C.PENALTY_DEVELOPER)
        retained = total_in - total_out

        def event_for(instr_row):
            key = instr_row["idem_key"]
            for et, ev in events.items():
                if key.endswith(f":{et}") or (et in key):
                    return {"event_id": ev["id"], "event_type": ev["event_type"],
                            "confirmed_by": ev["confirmed_by"], "confirmed_at": ev["confirmed_at"]}
            if key.startswith("ref:") or key.startswith("pen:"):
                # 退款/罚没指令关联最近一个终止类事件
                for et in (C.EVENT_OVERDUE, C.EVENT_DEVELOPER_BREACH, C.EVENT_CANCEL_DEVELOPER,
                           C.EVENT_BUYER_BREACH, C.EVENT_CANCEL_BUYER):
                    if et in events:
                        ev = events[et]
                        return {"event_id": ev["id"], "event_type": ev["event_type"],
                                "confirmed_by": ev["confirmed_by"], "confirmed_at": ev["confirmed_at"]}
            return None

        trail = {
            "state": "ok",
            "contract_id": contract["id"],
            "contract_no": contract["contract_no"],
            "contract_status": contract["status"],
            "inflows": [{"instr_id": r["id"], "amount_yuan": from_cents(r["amount_cents"]),
                         "status": r["status"], "bank_ref": r["bank_ref"],
                         "callback_id": r["cb"], "received_at": r["cb_at"]} for r in inflows],
            "outflows": [{"instr_id": r["id"], "flow_type": r["flow_type"],
                          "amount_yuan": from_cents(r["amount_cents"]),
                          "destination": r["destination"], "reason": r["reason"],
                          "status": r["status"], "bank_ref": r["bank_ref"],
                          "callback_id": r["cb"], "received_at": r["cb_at"],
                          "basis": event_for(r)} for r in outflows],
            "ledger": ledger_out,
            "reconciliation": {
                "total_in_yuan": from_cents(total_in),
                "escrow_out_settled_yuan": from_cents(total_out),
                "developer_penalty_outside_escrow_yuan": from_cents(dev_penalty_out),
                "retained_for_contract_yuan": from_cents(retained),
                "balanced": retained >= 0,
            },
        }
        return trail
