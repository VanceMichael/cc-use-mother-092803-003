"""预售资金监管领域服务。

事件 → 确定结果的主线：

  签约(应收: 定金/首付/按揭)
    └─ 银行入账回调（去重）→ 资金进入监管账户
  主体封顶核定 ──▶ 可按累计 60% 申请解冻
  竣工验收核定 ──▶ 累计可解冻至 100%
  取消 / 买方违约 / 开发商违约 / 逾期 ──▶ 一次性确定结算（退款或没收）

三道闸共同防止提前放款：
  1. 节点未按顺序核定，不能申请/审批对应节点的拨款；
  2. 合同进入终态后，任何待审批拨款一律拒绝；
  3. 出账时逐桶检查可用余额，监管账户不得透支。

银行回调以 (event_type, bank_serial) 去重，重送返回原结果、绝不重复划款。
"""
from __future__ import annotations

from threading import RLock

from .contracts import (
    FEE_DEPOSIT, FEE_DOWN_PAYMENT, FEE_MORTGAGE, FEE_LABELS,
    DEPOSIT_RATE,
    NODE_FLOOR_TOPOUT, NODE_ACCEPTANCE, NODE_ORDER, NODE_LABELS, NODE_RELEASE_CUM_RATE,
    RECV_PENDING, RECV_RECEIVED, RECV_RELEASED, RECV_REFUNDED,
    CONTRACT_ACTIVE, CONTRACT_CANCELLED, CONTRACT_BUYER_BREACH,
    CONTRACT_DEVELOPER_BREACH, CONTRACT_OVERDUE, CONTRACT_TERMINAL, CONTRACT_LABELS,
    REL_PENDING, REL_APPROVED, REL_PAID, REL_REJECTED, REL_FAILED,
    LEDGER_RECEIVE, LEDGER_RELEASE, LEDGER_REFUND, LEDGER_FORFEIT,
    ROLE_ADMIN, ROLE_DEVELOPER, ROLE_REGULATOR, ROLE_BANK, ROLE_AUDITOR, ROLE_BUYER,
    Request, Result, rejected, ok, validate_request, require_amount,
)
from .store import Store


class HousingEscrowService:
    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store()
        self._lock = RLock()

    # ======================================================================
    # 入口
    # ======================================================================

    def handle(self, request: Request) -> Result:
        validate_request(request)
        with self._lock:
            try:
                cached = self.store.cached_result(request.request_id)
                if cached is not None:
                    accepted, state, message, data = cached
                    return Result(accepted, state, message, data)

                user = self.store.query_one(
                    "SELECT * FROM users WHERE username=?", (request.actor,))
                if user is None:
                    # 系统自举：用户表为空时，允许创建首个且必须为 admin 的用户
                    count = self.store.query_one("SELECT COUNT(*) AS c FROM users")["c"]
                    if (count == 0 and request.action == "create_user"
                            and request.payload.get("role") == ROLE_ADMIN
                            and request.payload.get("username") == request.actor):
                        result = self._create_user(
                            {"role": ROLE_ADMIN, "project_id": None}, request.payload)
                    else:
                        result = rejected("未知用户，拒绝访问", "unauthorized")
                else:
                    result = self._dispatch(user, request)
            except ValueError as exc:
                self.store.rollback()
                result = rejected(str(exc), "invalid_request")
            except Exception:
                self.store.rollback()
                raise

            self.store.save_result(request.request_id, result.accepted,
                                   result.state, result.message, result.data)
            self.store.execute(
                "INSERT INTO audit_log (request_id, actor, action, accepted, summary, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (request.request_id, request.actor, request.action,
                 int(result.accepted), result.message, self.store.now()),
            )
            self.store.commit()
            return result

    def _dispatch(self, user, request: Request) -> Result:
        action = request.action
        p = request.payload
        # ---- 用户与基础数据维护 ----
        if action == "create_user":
            return self._create_user(user, p)
        if action == "create_project":
            return self._create_project(user, p)
        if action == "create_account":
            return self._create_account(user, p)
        if action == "create_unit":
            return self._create_unit(user, p)
        # ---- 合同与应收 ----
        if action == "sign_contract":
            return self._sign_contract(user, p)
        # ---- 银行回调 ----
        if action == "bank_inbound":
            return self._bank_inbound(user, p)
        if action == "bank_outbound_result":
            return self._bank_outbound_result(user, p)
        # ---- 节点与解冻 ----
        if action == "attest_node":
            return self._attest_node(user, p)
        if action == "request_release":
            return self._request_release(user, p)
        if action == "approve_release":
            return self._approve_release(user, p, approve=True)
        if action == "reject_release":
            return self._approve_release(user, p, approve=False)
        # ---- 违约 / 取消 / 逾期 ----
        if action == "register_event":
            return self._register_event(user, p)
        # ---- 查询 ----
        if action == "project_overview":
            return self._project_overview(user, p)
        if action == "contract_detail":
            return self._contract_detail(user, p)
        if action == "fund_trace":
            return self._fund_trace(user, p)
        return rejected(f"未知动作: {action}", "unknown_action")

    # ======================================================================
    # 权限工具
    # ======================================================================

    @staticmethod
    def _require_role(user, *roles: str) -> Result | None:
        if user["role"] not in roles:
            return rejected(f"角色 {user['role']} 无权执行该操作", "forbidden")
        return None

    def _project_scope(self, user, project_id: str) -> Result | None:
        """admin/auditor 可跨项目；developer/regulator/bank 只能操作被分配的项目。"""
        if user["role"] in (ROLE_ADMIN, ROLE_AUDITOR):
            return None
        if user["project_id"] != project_id:
            return rejected("该项目不在本人职责范围内", "out_of_scope")
        return None

    def _require_project(self, project_id: str):
        row = self.store.query_one("SELECT * FROM projects WHERE project_id=?", (project_id,))
        if row is None:
            raise ValueError(f"项目不存在: {project_id}")
        return row

    def _account_of_project(self, project_id: str):
        row = self.store.query_one(
            "SELECT * FROM escrow_accounts WHERE project_id=?", (project_id,))
        if row is None:
            raise ValueError(f"项目 {project_id} 尚未开立监管账户")
        return row

    def _require_contract(self, contract_no: str):
        row = self.store.query_one("SELECT * FROM contracts WHERE contract_no=?", (contract_no,))
        if row is None:
            raise ValueError(f"合同不存在: {contract_no}")
        return row

    # ======================================================================
    # 基础数据维护
    # ======================================================================

    def _create_user(self, user, p) -> Result:
        denied = self._require_role(user, ROLE_ADMIN)
        if denied:
            return denied
        username = str(p.get("username", "")).strip()
        role = str(p.get("role", "")).strip()
        if not username:
            raise ValueError("缺少用户名")
        if role not in (ROLE_ADMIN, ROLE_DEVELOPER, ROLE_REGULATOR, ROLE_BANK,
                        ROLE_AUDITOR, ROLE_BUYER):
            raise ValueError(f"非法角色: {role}")
        if role in (ROLE_DEVELOPER, ROLE_REGULATOR, ROLE_BANK) and not p.get("project_id"):
            raise ValueError(f"{role} 必须绑定项目")
        if role == ROLE_BUYER and not p.get("buyer_id"):
            raise ValueError("购房人用户必须绑定 buyer_id")
        try:
            self.store.execute(
                "INSERT INTO users (username, role, project_id, buyer_id, display_name, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (username, role, p.get("project_id"), p.get("buyer_id"),
                 str(p.get("display_name", "")), self.store.now()),
            )
        except Exception as exc:  # 唯一约束冲突
            raise ValueError(f"用户已存在: {username}") from exc
        return ok("用户已创建", "created", {"username": username, "role": role})

    def _create_project(self, user, p) -> Result:
        denied = self._require_role(user, ROLE_ADMIN)
        if denied:
            return denied
        project_id = str(p.get("project_id", "")).strip()
        name = str(p.get("name", "")).strip()
        if not project_id or not name:
            raise ValueError("缺少项目编号或名称")
        try:
            self.store.execute(
                "INSERT INTO projects (project_id, name, status, created_at) VALUES (?,?,?,?)",
                (project_id, name, "active", self.store.now()))
        except Exception as exc:
            raise ValueError(f"项目已存在: {project_id}") from exc
        return ok("项目已创建", "created", {"project_id": project_id})

    def _create_account(self, user, p) -> Result:
        denied = self._require_role(user, ROLE_ADMIN)
        if denied:
            return denied
        account_no = str(p.get("account_no", "")).strip()
        project_id = str(p.get("project_id", "")).strip()
        bank_name = str(p.get("bank_name", "")).strip()
        if not account_no or not project_id or not bank_name:
            raise ValueError("缺少账号、项目或开户行")
        self._require_project(project_id)
        if self.store.query_one("SELECT 1 FROM escrow_accounts WHERE project_id=?",
                                (project_id,)):
            raise ValueError("该项目已开立监管账户")
        self.store.execute(
            "INSERT INTO escrow_accounts (account_no, project_id, bank_name, balance, created_at)"
            " VALUES (?,?,?,?,?)",
            (account_no, project_id, bank_name, 0, self.store.now()))
        return ok("监管账户已开立", "created",
                  {"account_no": account_no, "project_id": project_id})

    def _create_unit(self, user, p) -> Result:
        denied = self._require_role(user, ROLE_ADMIN, ROLE_DEVELOPER)
        if denied:
            return denied
        project_id = str(p.get("project_id", "")).strip()
        unit_no = str(p.get("unit_no", "")).strip()
        building = str(p.get("building", "")).strip()
        total_price = require_amount(p, "total_price")[0]
        self._require_project(project_id)
        if denied := self._project_scope(user, project_id):
            return denied
        try:
            self.store.execute(
                "INSERT INTO units (project_id, unit_no, building, total_price, status)"
                " VALUES (?,?,?,?,?)",
                (project_id, unit_no, building, total_price, "available"))
        except Exception as exc:
            raise ValueError(f"房源已存在: {unit_no}") from exc
        return ok("房源已备案", "created",
                  {"project_id": project_id, "unit_no": unit_no, "total_price": total_price})

    # ======================================================================
    # 签约：生成定金/首付/按揭应收，强制定金 ≤ 3%
    # ======================================================================

    def _sign_contract(self, user, p) -> Result:
        denied = self._require_role(user, ROLE_DEVELOPER)
        if denied:
            return denied
        project_id = str(p.get("project_id", "")).strip()
        unit_no = str(p.get("unit_no", "")).strip()
        contract_no = str(p.get("contract_no", "")).strip()
        buyer_id = str(p.get("buyer_id", "")).strip()
        buyer_name = str(p.get("buyer_name", "")).strip()
        deposit = require_amount(p, "deposit")[0]
        down_payment = require_amount(p, "down_payment")[0]
        mortgage = int(p.get("mortgage", 0) or 0)
        if not contract_no or not buyer_id:
            raise ValueError("缺少合同号或购房人编号")
        if denied := self._project_scope(user, project_id):
            return denied

        unit = self.store.query_one(
            "SELECT * FROM units WHERE project_id=? AND unit_no=?", (project_id, unit_no))
        if unit is None:
            raise ValueError(f"房源不存在: {unit_no}")
        if unit["status"] != "available":
            return rejected("房源已签约或不可售", "unit_unavailable")

        total_price = unit["total_price"]
        deposit_cap = total_price * DEPOSIT_RATE // 100
        if deposit > deposit_cap:
            return rejected(
                f"定金 {deposit} 分超过总价 {DEPOSIT_RATE}% 上限（{deposit_cap} 分）",
                "deposit_cap_exceeded",
                {"deposit": deposit, "cap": deposit_cap, "total_price": total_price})
        if deposit + down_payment + mortgage != total_price:
            return rejected(
                "定金+首付款+按揭贷款必须等于备案总价",
                "amount_mismatch",
                {"sum": deposit + down_payment + mortgage, "total_price": total_price})
        if down_payment <= 0:
            raise ValueError("首付款必须为正数")

        self.store.execute(
            """INSERT INTO contracts
               (contract_no, project_id, unit_no, buyer_id, buyer_name, total_price,
                status, created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (contract_no, project_id, unit_no, buyer_id, buyer_name, total_price,
             CONTRACT_ACTIVE, self.store.now()))
        for fee_type, amount in ((FEE_DEPOSIT, deposit),
                                 (FEE_DOWN_PAYMENT, down_payment),
                                 (FEE_MORTGAGE, mortgage)):
            if amount <= 0:
                continue
            self.store.execute(
                "INSERT INTO receivables (contract_no, fee_type, due_amount, status)"
                " VALUES (?,?,?,?)",
                (contract_no, fee_type, amount, RECV_PENDING))
        self.store.execute(
            "UPDATE units SET status='signed' WHERE project_id=? AND unit_no=?",
            (project_id, unit_no))
        return ok("合同已网签，等待资金入账", CONTRACT_ACTIVE, {
            "contract_no": contract_no, "total_price": total_price,
            "deposit": deposit, "deposit_cap": deposit_cap,
            "down_payment": down_payment, "mortgage": mortgage})

    # ======================================================================
    # 银行入账回调（重送去重）
    # ======================================================================

    def _bank_inbound(self, user, p) -> Result:
        denied = self._require_role(user, ROLE_BANK)
        if denied:
            return denied
        contract_no = str(p.get("contract_no", "")).strip()
        fee_type = str(p.get("fee_type", "")).strip()
        bank_serial = str(p.get("bank_serial", "")).strip()
        amount = require_amount(p, "amount")[0]
        if fee_type not in (FEE_DEPOSIT, FEE_DOWN_PAYMENT, FEE_MORTGAGE):
            raise ValueError("费用类别非法")
        if not bank_serial:
            raise ValueError("缺少银行流水号")

        contract = self._require_contract(contract_no)
        if denied := self._project_scope(user, contract["project_id"]):
            return denied
        if self.store.bank_event_seen("inbound", bank_serial, dict(p)):
            return ok("该笔入账回调已处理，未重复入账", "duplicate_callback",
                      {"bank_serial": bank_serial})

        recv = self.store.query_one(
            "SELECT * FROM receivables WHERE contract_no=? AND fee_type=?",
            (contract_no, fee_type))
        if recv is None:
            raise ValueError(f"合同无 {FEE_LABELS.get(fee_type, fee_type)} 应收记录")
        if recv["status"] != RECV_PENDING:
            return rejected(f"{FEE_LABELS[fee_type]}已入账，不得重复入账",
                            "already_received", {"fee_type": fee_type})
        if amount != recv["due_amount"]:
            return rejected(
                "到账金额与应收金额不一致",
                "amount_mismatch",
                {"due": recv["due_amount"], "received": amount})

        account = self._account_of_project(contract["project_id"])
        self.store.execute(
            "UPDATE receivables SET status=?, received_amount=?, bank_serial=?, received_at=?"
            " WHERE id=?",
            (RECV_RECEIVED, amount, bank_serial, self.store.now(), recv["id"]))
        self.store.append_ledger(
            contract_no=contract_no, project_id=contract["project_id"],
            account_no=account["account_no"], fee_type=fee_type,
            direction=LEDGER_RECEIVE, amount=amount, bank_serial=bank_serial,
            ref_type="receivable", ref_id=recv["id"],
            reason=f"{FEE_LABELS[fee_type]}进入监管账户")
        return ok(f"{FEE_LABELS[fee_type]}已确认进入监管账户", RECV_RECEIVED, {
            "contract_no": contract_no, "fee_type": fee_type,
            "amount": amount, "bank_serial": bank_serial,
            "balance": account["balance"] + amount})

    # ======================================================================
    # 节点核定（必须按顺序：封顶 → 验收）
    # ======================================================================

    def _attest_node(self, user, p) -> Result:
        denied = self._require_role(user, ROLE_REGULATOR)
        if denied:
            return denied
        project_id = str(p.get("project_id", "")).strip()
        node = str(p.get("node", "")).strip()
        evidence = str(p.get("evidence", "")).strip()
        if node not in NODE_ORDER:
            raise ValueError(f"非法节点: {node}")
        if denied := self._project_scope(user, project_id):
            return denied
        self._require_project(project_id)
        if self.store.query_one(
                "SELECT 1 FROM node_attestations WHERE project_id=? AND node=?",
                (project_id, node)):
            return rejected(f"{NODE_LABELS[node]}已核定，不得重复核定", "node_duplicate")
        idx = NODE_ORDER.index(node)
        if idx > 0:
            prev = NODE_ORDER[idx - 1]
            if not self.store.query_one(
                    "SELECT 1 FROM node_attestations WHERE project_id=? AND node=?",
                    (project_id, prev)):
                return rejected(
                    f"必须先完成{NODE_LABELS[prev]}核定，才能核定{NODE_LABELS[node]}",
                    "node_out_of_order", {"required_node": prev})
        self.store.execute(
            "INSERT INTO node_attestations (project_id, node, attested_at, attested_by, evidence)"
            " VALUES (?,?,?,?,?)",
            (project_id, node, self.store.now(), user["username"], evidence))
        return ok(f"{NODE_LABELS[node]}已核定", "node_attested",
                  {"project_id": project_id, "node": node,
                   "cumulative_release_rate": NODE_RELEASE_CUM_RATE[node]})

    # ---- 可解冻额度：节点累计比例 × 总价 − 已申请未驳回 − 已拨付 ------------

    def _release_quota(self, contract, exclude_id: int | None = None) -> int:
        attested = self.store.query_all(
            "SELECT node FROM node_attestations WHERE project_id=?",
            (contract["project_id"],))
        nodes = {r["node"] for r in attested}
        rate = 0
        for node in NODE_ORDER:
            if node in nodes:
                rate = NODE_RELEASE_CUM_RATE[node]
        cumulative_allowed = contract["total_price"] * rate // 100
        row = self.store.query_one(
            "SELECT COALESCE(SUM(amount),0) AS used FROM outgo_items"
            " WHERE contract_no=? AND kind='node_release' AND status IN (?,?,?)"
            + (" AND id<>?" if exclude_id is not None else ""),
            ((contract["contract_no"], REL_PENDING, REL_APPROVED, REL_PAID, exclude_id)
             if exclude_id is not None
             else (contract["contract_no"], REL_PENDING, REL_APPROVED, REL_PAID)))
        return cumulative_allowed - int(row["used"])

    def _request_release(self, user, p) -> Result:
        denied = self._require_role(user, ROLE_DEVELOPER)
        if denied:
            return denied
        contract_no = str(p.get("contract_no", "")).strip()
        node = str(p.get("node", "")).strip()
        amount = require_amount(p, "amount")[0]
        reason = str(p.get("reason", "")).strip()
        if node not in NODE_ORDER:
            raise ValueError("解冻节点非法")
        contract = self._require_contract(contract_no)
        if denied := self._project_scope(user, contract["project_id"]):
            return denied
        if contract["status"] in CONTRACT_TERMINAL:
            return rejected(f"合同已{CONTRACT_LABELS[contract['status']]}，禁止解冻放款",
                            "contract_terminal")
        if not self.store.query_one(
                "SELECT 1 FROM node_attestations WHERE project_id=? AND node=?",
                (contract["project_id"], node)):
            return rejected(
                f"{NODE_LABELS[node]}尚未核定，不能提前申请放款",
                "node_not_attested", {"node": node})
        quota = self._release_quota(contract)
        if amount > quota:
            return rejected(
                f"申请解冻 {amount} 分超过节点可用额度 {quota} 分",
                "quota_exceeded", {"available_quota": quota})
        cur = self.store.execute(
            """INSERT INTO outgo_items
               (kind, payee, fee_type, contract_no, project_id, node, amount,
                status, reason, created_by, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            ("node_release", "developer", "", contract_no, contract["project_id"],
             node, amount, REL_PENDING, reason, user["username"], self.store.now()))
        return ok("解冻申请已提交，等待监管审批", REL_PENDING, {
            "outgo_id": cur.lastrowid, "contract_no": contract_no,
            "node": node, "amount": amount, "available_quota_after": quota - amount})

    @staticmethod
    def _outgo_id(p) -> int:
        try:
            return int(p["outgo_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("缺少有效的出账编号 outgo_id") from exc

    def _approve_release(self, user, p, approve: bool) -> Result:
        denied = self._require_role(user, ROLE_REGULATOR)
        if denied:
            return denied
        outgo_id = self._outgo_id(p)
        item = self.store.query_one("SELECT * FROM outgo_items WHERE id=?", (outgo_id,))
        if item is None:
            raise ValueError("出账申请不存在")
        if item["kind"] != "node_release":
            return rejected("该出账不是节点解冻申请", "wrong_kind")
        if denied := self._project_scope(user, item["project_id"]):
            return denied
        if item["status"] != REL_PENDING:
            return rejected(f"申请状态为 {item['status']}，不能再审批",
                            "not_pending", {"status": item["status"]})
        contract = self._require_contract(item["contract_no"])
        if not approve:
            self._set_outgo(item["id"], REL_REJECTED,
                            fail_reason=str(p.get("reason", "")))
            return ok("解冻申请已驳回", REL_REJECTED, {"outgo_id": outgo_id})

        # 审批前二次校验：合同必须仍在履约，且额度仍有效（防止核定后又被结算）
        if contract["status"] in CONTRACT_TERMINAL:
            self._set_outgo(item["id"], REL_REJECTED, fail_reason="合同已终止")
            return rejected(f"合同已{CONTRACT_LABELS[contract['status']]}，拒绝放款",
                            "contract_terminal")
        quota = self._release_quota(contract, exclude_id=item["id"])
        if item["amount"] > quota:
            self._set_outgo(item["id"], REL_REJECTED, fail_reason="额度已被占用")
            return rejected("可解冻额度不足，拒绝放款", "quota_exceeded",
                            {"available_quota": quota})
        self._set_outgo(item["id"], REL_APPROVED, approver=user["username"])
        return ok("解冻申请已批准，等待银行划款回调", REL_APPROVED,
                  {"outgo_id": outgo_id, "amount": item["amount"]})

    def _set_outgo(self, outgo_id: int, status: str, *, approver: str | None = None,
                   bank_serial: str | None = None, fail_reason: str = "") -> None:
        if status == REL_APPROVED:
            self.store.execute(
                "UPDATE outgo_items SET status=?, approved_by=?, approved_at=? WHERE id=?",
                (status, approver, self.store.now(), outgo_id))
        elif status == REL_PAID:
            self.store.execute(
                "UPDATE outgo_items SET status=?, bank_serial=?, paid_at=? WHERE id=?",
                (status, bank_serial, self.store.now(), outgo_id))
        else:  # rejected / failed
            self.store.execute(
                "UPDATE outgo_items SET status=?, fail_reason=? WHERE id=?",
                (status, fail_reason, outgo_id))

    # ======================================================================
    # 银行出账结果回调（划款成功/失败；重送去重；绝不重复划款）
    # ======================================================================

    def _bank_outbound_result(self, user, p) -> Result:
        denied = self._require_role(user, ROLE_BANK)
        if denied:
            return denied
        outgo_id = self._outgo_id(p)
        bank_serial = str(p.get("bank_serial", "")).strip()
        success = bool(p.get("success"))
        if not bank_serial:
            raise ValueError("缺少银行流水号")
        item = self.store.query_one("SELECT * FROM outgo_items WHERE id=?", (outgo_id,))
        if item is None:
            raise ValueError("出账申请不存在")
        if denied := self._project_scope(user, item["project_id"]):
            return denied

        if self.store.bank_event_seen(item["kind"], bank_serial, dict(p)):
            # 重送：返回当前处理结果，不再次移动资金
            fresh = self.store.query_one("SELECT * FROM outgo_items WHERE id=?", (outgo_id,))
            return ok("该划款回调已处理，未重复划款", fresh["status"],
                      {"outgo_id": outgo_id, "status": fresh["status"],
                       "bank_serial": bank_serial, "duplicate": True})

        if item["status"] == REL_PAID:
            return ok("该出账此前已划款成功，未重复划款", REL_PAID,
                      {"outgo_id": outgo_id, "duplicate": True})
        if item["status"] != REL_APPROVED:
            return rejected(f"出账状态为 {item['status']}，银行不能划款",
                            "not_approved", {"status": item["status"]})

        contract = self._require_contract(item["contract_no"])
        account = self._account_of_project(item["project_id"])

        # 终态拦截：节点拨款若在合同终止前已批准但尚未划款，此时必须拦下，
        # 资金留在监管账户等待结算出账（refund/forfeit 不受此限）。
        if item["kind"] == "node_release" and contract["status"] in CONTRACT_TERMINAL:
            self._set_outgo(item["id"], REL_FAILED,
                            fail_reason=f"合同已{CONTRACT_LABELS[contract['status']]}，停止放款")
            return rejected(
                f"合同已{CONTRACT_LABELS[contract['status']]}，该笔节点拨款停止划款",
                "contract_terminal", {"balance": account["balance"]})

        if not success:
            self._set_outgo(item["id"], REL_FAILED,
                            fail_reason=str(p.get("fail_reason", "银行划款失败")))
            return ok("银行划款失败，资金仍在监管账户", REL_FAILED,
                      {"outgo_id": outgo_id, "balance": account["balance"]})

        if item["kind"] == "node_release":
            return self._pay_node_release(item, contract, account, bank_serial)
        if item["kind"] == "forfeit":
            return self._pay_forfeit(item, contract, account, bank_serial)
        if item["kind"] == "refund":
            return self._pay_refund(item, contract, account, bank_serial)
        return rejected(f"未知出账类型: {item['kind']}", "wrong_kind")

    # ---- 节点拨款：按费用桶先进先出扣减（定金/首付/按揭） --------------------

    def _pay_node_release(self, item, contract, account, bank_serial) -> Result:
        remaining = item["amount"]
        buckets = self.store.query_all(
            "SELECT * FROM receivables WHERE contract_no=? AND status=? ORDER BY id",
            (contract["contract_no"], RECV_RECEIVED))
        plan: list[tuple] = []
        for recv in buckets:
            available = recv["received_amount"] - recv["released_amount"] - recv["refunded_amount"]
            if available <= 0:
                continue
            take = min(available, remaining)
            if take:
                plan.append((recv, take))
                remaining -= take
            if remaining == 0:
                break
        if remaining > 0:
            # 监管账户内属于该合同的可用资金不足（透支拦截前置）
            self._set_outgo(item["id"], REL_FAILED, fail_reason="合同监管可用资金不足")
            return rejected("合同在监管账户内的可用资金不足，拒绝划款",
                            "insufficient_funds", {"shortage": remaining})

        for recv, take in plan:
            new_released = recv["released_amount"] + take
            status = RECV_RELEASED if new_released >= recv["received_amount"] else recv["status"]
            self.store.execute(
                "UPDATE receivables SET released_amount=?, status=? WHERE id=?",
                (new_released, status, recv["id"]))
            self.store.append_ledger(
                contract_no=contract["contract_no"], project_id=contract["project_id"],
                account_no=account["account_no"], fee_type=recv["fee_type"],
                direction=LEDGER_RELEASE, amount=take, bank_serial=bank_serial,
                ref_type="outgo_item", ref_id=item["id"],
                reason=f"{NODE_LABELS.get(item['node'], item['node'])}节点解冻拨付开发商")
        self._set_outgo(item["id"], REL_PAID, bank_serial=bank_serial)
        row = self.store.query_one("SELECT balance FROM escrow_accounts WHERE account_no=?",
                                   (account["account_no"],))
        return ok("节点资金已拨付开发商", REL_PAID, {
            "outgo_id": item["id"], "amount": item["amount"],
            "bank_serial": bank_serial, "balance": row["balance"],
            "breakdown": [{"fee_type": r["fee_type"], "amount": t} for r, t in plan]})

    def _pay_forfeit(self, item, contract, account, bank_serial) -> Result:
        recv = self.store.query_one(
            "SELECT * FROM receivables WHERE contract_no=? AND fee_type=?",
            (contract["contract_no"], item["fee_type"]))
        available = (recv["received_amount"] - recv["released_amount"]
                     - recv["refunded_amount"]) if recv else 0
        if item["amount"] > available:
            self._set_outgo(item["id"], REL_FAILED, fail_reason="定金余额不足")
            return rejected("可没收的定金不足", "insufficient_funds")
        new_released = recv["released_amount"] + item["amount"]
        self.store.execute("UPDATE receivables SET released_amount=? WHERE id=?",
                           (new_released, recv["id"]))
        self.store.append_ledger(
            contract_no=contract["contract_no"], project_id=contract["project_id"],
            account_no=account["account_no"], fee_type=recv["fee_type"],
            direction=LEDGER_FORFEIT, amount=item["amount"], bank_serial=bank_serial,
            ref_type="outgo_item", ref_id=item["id"],
            reason=item["reason"] or "买方违约，定金没收拨付开发商")
        self._set_outgo(item["id"], REL_PAID, bank_serial=bank_serial)
        row = self.store.query_one("SELECT balance FROM escrow_accounts WHERE account_no=?",
                                   (account["account_no"],))
        return ok("定金已没收并拨付开发商", REL_PAID,
                  {"outgo_id": item["id"], "amount": item["amount"], "balance": row["balance"]})

    def _pay_refund(self, item, contract, account, bank_serial) -> Result:
        recv = self.store.query_one(
            "SELECT * FROM receivables WHERE contract_no=? AND fee_type=?",
            (contract["contract_no"], item["fee_type"]))
        available = (recv["received_amount"] - recv["released_amount"]
                     - recv["refunded_amount"]) if recv else 0
        if item["amount"] > available:
            self._set_outgo(item["id"], REL_FAILED, fail_reason="可退资金不足")
            return rejected(
                f"{FEE_LABELS.get(item['fee_type'], item['fee_type'])}可退余额不足",
                "insufficient_funds", {"available": available})
        new_refunded = recv["refunded_amount"] + item["amount"]
        status = RECV_REFUNDED if new_refunded >= recv["received_amount"] else recv["status"]
        self.store.execute(
            "UPDATE receivables SET refunded_amount=?, status=? WHERE id=?",
            (new_refunded, status, recv["id"]))
        self.store.append_ledger(
            contract_no=contract["contract_no"], project_id=contract["project_id"],
            account_no=account["account_no"], fee_type=recv["fee_type"],
            direction=LEDGER_REFUND, amount=item["amount"], bank_serial=bank_serial,
            ref_type="outgo_item", ref_id=item["id"],
            reason=item["reason"] or "解除合同，退还购房人")
        self._set_outgo(item["id"], REL_PAID, bank_serial=bank_serial)
        row = self.store.query_one("SELECT balance FROM escrow_accounts WHERE account_no=?",
                                   (account["account_no"],))
        return ok("资金已退还购房人", REL_PAID,
                  {"outgo_id": item["id"], "fee_type": item["fee_type"],
                   "amount": item["amount"], "balance": row["balance"]})

    # ======================================================================
    # 取消 / 违约 / 逾期：一次性确定结算
    # ======================================================================

    _EVENT_LABELS = {
        CONTRACT_CANCELLED: "合同取消",
        CONTRACT_BUYER_BREACH: "买方违约",
        CONTRACT_DEVELOPER_BREACH: "开发商违约",
        CONTRACT_OVERDUE: "项目逾期",
    }

    def _register_event(self, user, p) -> Result:
        denied = self._require_role(user, ROLE_REGULATOR)
        if denied:
            return denied
        contract_no = str(p.get("contract_no", "")).strip()
        event = str(p.get("event", "")).strip()
        reason = str(p.get("reason", "")).strip()
        if event not in self._EVENT_LABELS:
            raise ValueError(
                "事件必须是 cancelled / buyer_breach / developer_breach / overdue")
        contract = self._require_contract(contract_no)
        if denied := self._project_scope(user, contract["project_id"]):
            return denied
        if contract["status"] in CONTRACT_TERMINAL:
            return rejected(f"合同已处于终态 {CONTRACT_LABELS[contract['status']]}",
                            "contract_terminal")

        # 1) 合同立即进入终态，冻结后续放款
        self.store.execute("UPDATE contracts SET status=? WHERE contract_no=?",
                           (event, contract_no))
        # 2) 所有未审批的节点拨款一律驳回；已批准未划款的由银行回调时按终态拦截
        pending = self.store.query_all(
            "SELECT id FROM outgo_items WHERE contract_no=? AND kind='node_release'"
            " AND status=?", (contract_no, REL_PENDING))
        for row in pending:
            self._set_outgo(row["id"], REL_REJECTED,
                            fail_reason=f"{self._EVENT_LABELS[event]}，终止解冻")

        # 3) 生成确定性的结算出账（只针对仍留在监管账户内的资金）
        plan = self._settlement_plan(contract, event, reason)
        created = []
        for kind, fee_type, amount, payee, why in plan:
            cur = self.store.execute(
                """INSERT INTO outgo_items
                   (kind, payee, fee_type, contract_no, project_id, node, amount,
                    status, reason, created_by, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (kind, payee, fee_type, contract_no, contract["project_id"], "",
                 amount, REL_APPROVED, why, user["username"], self.store.now()))
            created.append({"outgo_id": cur.lastrowid, "kind": kind,
                           "fee_type": fee_type, "amount": amount, "payee": payee})

        # 项目逾期同时影响全部履约中合同；此处只登记单合同，项目级逾期单独入口
        return ok(f"{self._EVENT_LABELS[event]}已登记，结算指令已生成（待银行划款回调）",
                  event, {"contract_no": contract_no, "settlement": created})

    def _settlement_plan(self, contract, event: str, reason: str) -> list[tuple]:
        recvs = self.store.query_all(
            "SELECT * FROM receivables WHERE contract_no=? ORDER BY id",
            (contract["contract_no"],))
        plan: list[tuple] = []
        for recv in recvs:
            held = recv["received_amount"] - recv["released_amount"] - recv["refunded_amount"]
            if held <= 0:
                continue
            if event == CONTRACT_BUYER_BREACH and recv["fee_type"] == FEE_DEPOSIT:
                plan.append(("forfeit", FEE_DEPOSIT, held, "developer",
                             reason or "买方违约，定金不退，没收拨付开发商"))
            else:
                # cancelled / 开发商违约 / 逾期：监管账户内全部资金退还购房人
                plan.append(("refund", recv["fee_type"], held, "buyer",
                             reason or f"{self._EVENT_LABELS[event]}，退还购房人"))
        return plan

    # ======================================================================
    # 查询：角色 + 项目范围隔离；资金全链路还原
    # ======================================================================

    def _scope_projects(self, user) -> list[str] | None:
        """返回允许查看的 project_id 列表；None 表示不限制。"""
        if user["role"] in (ROLE_ADMIN, ROLE_AUDITOR, ROLE_REGULATOR):
            # regulator 仍限定本人项目；admin/auditor 全量
            if user["role"] == ROLE_REGULATOR:
                return [user["project_id"]] if user["project_id"] else []
            return None
        if user["role"] in (ROLE_DEVELOPER, ROLE_BANK):
            return [user["project_id"]] if user["project_id"] else []
        return []  # buyer 不按项目维度查询

    def _can_view_contract(self, user, contract) -> bool:
        if user["role"] in (ROLE_ADMIN, ROLE_AUDITOR):
            return True
        if user["role"] in (ROLE_DEVELOPER, ROLE_REGULATOR, ROLE_BANK):
            return user["project_id"] == contract["project_id"]
        if user["role"] == ROLE_BUYER:
            return user["buyer_id"] == contract["buyer_id"]
        return False

    def _project_overview(self, user, p) -> Result:
        if user["role"] not in (ROLE_ADMIN, ROLE_AUDITOR, ROLE_REGULATOR, ROLE_DEVELOPER,
                                ROLE_BANK):
            return rejected("无权查看项目总览", "forbidden")
        wanted = str(p.get("project_id", "")).strip()
        if wanted:
            denied = self._project_scope(user, wanted)
            if denied:
                return denied
            projects = [self._require_project(wanted)]
        else:
            scoped = self._scope_projects(user)
            if scoped is None:
                projects = self.store.query_all("SELECT * FROM projects ORDER BY project_id")
            elif scoped:
                qmarks = ",".join("?" * len(scoped))
                projects = self.store.query_all(
                    f"SELECT * FROM projects WHERE project_id IN ({qmarks}) ORDER BY project_id",
                    scoped)
            else:
                projects = []
        out = []
        for proj in projects:
            acc = self.store.query_one(
                "SELECT * FROM escrow_accounts WHERE project_id=?", (proj["project_id"],))
            nodes = [r["node"] for r in self.store.query_all(
                "SELECT node FROM node_attestations WHERE project_id=? ORDER BY id",
                (proj["project_id"],))]
            out.append({
                "project_id": proj["project_id"], "name": proj["name"],
                "status": proj["status"],
                "account": None if acc is None else {
                    "account_no": acc["account_no"], "bank_name": acc["bank_name"],
                    "balance": acc["balance"]},
                "attested_nodes": nodes,
            })
        return ok("查询成功", "listed", {"projects": out})

    def _contract_detail(self, user, p) -> Result:
        contract_no = str(p.get("contract_no", "")).strip()
        contract = self._require_contract(contract_no)
        if not self._can_view_contract(user, contract):
            return rejected("无权查看该合同（超出职责/本人范围）", "out_of_scope")
        recvs = [dict(r) for r in self.store.query_all(
            "SELECT fee_type, due_amount, received_amount, released_amount,"
            " refunded_amount, status, bank_serial FROM receivables"
            " WHERE contract_no=? ORDER BY id", (contract_no,))]
        releases = [dict(r) for r in self.store.query_all(
            "SELECT id AS outgo_id, kind, node, amount, status, payee, reason"
            " FROM outgo_items WHERE contract_no=? ORDER BY id", (contract_no,))]
        return ok("查询成功", contract["status"], {
            "contract_no": contract_no, "project_id": contract["project_id"],
            "unit_no": contract["unit_no"], "buyer_id": contract["buyer_id"],
            "buyer_name": contract["buyer_name"], "total_price": contract["total_price"],
            "status": contract["status"], "receivables": recvs, "outgo_items": releases})

    def _fund_trace(self, user, p) -> Result:
        contract_no = str(p.get("contract_no", "")).strip()
        contract = self._require_contract(contract_no)
        if not self._can_view_contract(user, contract):
            return rejected("无权查看该合同资金轨迹", "out_of_scope")
        entries = self.store.query_all(
            "SELECT fee_type, direction, amount, bank_serial, ref_type, ref_id,"
            " reason, created_at FROM ledger_entries WHERE contract_no=? ORDER BY id",
            (contract_no,))
        # 按费用桶汇总：进入多少、拨付多少、退还多少、当前仍在监管账户多少
        summary: dict[str, dict[str, int]] = {}
        for e in entries:
            bucket = summary.setdefault(e["fee_type"], {
                "received": 0, "released": 0, "refunded": 0, "forfeited": 0, "held": 0})
            if e["direction"] == LEDGER_RECEIVE:
                bucket["received"] += e["amount"]; bucket["held"] += e["amount"]
            elif e["direction"] == LEDGER_RELEASE:
                bucket["released"] += e["amount"]; bucket["held"] -= e["amount"]
            elif e["direction"] == LEDGER_REFUND:
                bucket["refunded"] += e["amount"]; bucket["held"] -= e["amount"]
            elif e["direction"] == LEDGER_FORFEIT:
                bucket["forfeited"] += e["amount"]; bucket["held"] -= e["amount"]
        return ok("资金全链路", contract["status"], {
            "contract_no": contract_no, "project_id": contract["project_id"],
            "total_price": contract["total_price"], "status": contract["status"],
            "summary_by_fee": summary,
            "trail": [dict(e) for e in entries]})
