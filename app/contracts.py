"""预售资金监管的输入输出约定与领域常量。

只使用 Python 标准库，便于在监管现场离线部署。
金额一律以「分」为单位的整数存储，杜绝浮点误差。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ---- 监管规则常量 ---------------------------------------------------------

DEPOSIT_RATE = 3          # 定金不得超过总价的 3%

# ---- 建设节点（必须按顺序核定） -------------------------------------------

NODE_FLOOR_TOPOUT = "floor_topout"   # 主体结构封顶
NODE_ACCEPTANCE = "acceptance"       # 竣工验收
NODE_ORDER = [NODE_FLOOR_TOPOUT, NODE_ACCEPTANCE]
NODE_LABELS = {
    NODE_FLOOR_TOPOUT: "主体结构封顶",
    NODE_ACCEPTANCE: "竣工验收",
}
# 各节点累计可解冻比例（占合同总价），未达到节点的资金不得放款
NODE_RELEASE_CUM_RATE = {
    NODE_FLOOR_TOPOUT: 60,
    NODE_ACCEPTANCE: 100,
}

# ---- 费用类别 -------------------------------------------------------------

FEE_DEPOSIT = "deposit"          # 定金（≤3%）
FEE_DOWN_PAYMENT = "down_payment"  # 首付款
FEE_MORTGAGE = "mortgage"        # 按揭贷款
FEE_LABELS = {
    FEE_DEPOSIT: "定金",
    FEE_DOWN_PAYMENT: "首付款",
    FEE_MORTGAGE: "按揭贷款",
}

# ---- 应收/流水状态 ---------------------------------------------------------

RECV_PENDING = "pending"      # 已签约待入账
RECV_RECEIVED = "received"    # 银行确认到账（在监管账户内）
RECV_RELEASED = "released"    # 已解冻拨付给开发商
RECV_REFUNDED = "refunded"    # 已退还购房人

# ---- 合同状态 -------------------------------------------------------------

CONTRACT_ACTIVE = "active"          # 履约中
CONTRACT_CANCELLED = "cancelled"    # 协商/买方原因取消
CONTRACT_BUYER_BREACH = "buyer_breach"    # 买方违约
CONTRACT_DEVELOPER_BREACH = "developer_breach"  # 开发商违约
CONTRACT_OVERDUE = "overdue"        # 项目逾期
CONTRACT_LABELS = {
    CONTRACT_ACTIVE: "履约中",
    CONTRACT_CANCELLED: "合同取消",
    CONTRACT_BUYER_BREACH: "买方违约",
    CONTRACT_DEVELOPER_BREACH: "开发商违约",
    CONTRACT_OVERDUE: "项目逾期",
}
# 终态：到达后不得再解冻或重复结算
CONTRACT_TERMINAL = {
    CONTRACT_CANCELLED,
    CONTRACT_BUYER_BREACH,
    CONTRACT_DEVELOPER_BREACH,
    CONTRACT_OVERDUE,
}

# ---- 解冻申请状态 ---------------------------------------------------------

REL_PENDING = "pending"      # 已提交，待监管人员核定/审批
REL_APPROVED = "approved"    # 已审批，待银行划款
REL_PAID = "paid"            # 银行回调确认已划款
REL_REJECTED = "rejected"    # 审批驳回
REL_FAILED = "failed"        # 银行划款失败，资金仍在监管账户

# ---- 台账（资金移动）类型与去向 -------------------------------------------

LEDGER_RECEIVE = "receive"    # 入监管账户
LEDGER_RELEASE = "release"    # 解冻拨付开发商
LEDGER_REFUND = "refund"      # 退还购房人
LEDGER_FORFEIT = "forfeit"    # 定金没收（仍拨付开发商）
LEDGER_TYPES = (LEDGER_RECEIVE, LEDGER_RELEASE, LEDGER_REFUND, LEDGER_FORFEIT)

# ---- 角色 -----------------------------------------------------------------

ROLE_ADMIN = "admin"            # 系统管理员：维护项目/账户/用户
ROLE_DEVELOPER = "developer"    # 项目公司（开发商）：录入房源合同、发起收款/解冻
ROLE_REGULATOR = "regulator"    # 监管人员：核定节点、审批解冻、登记违约逾期
ROLE_BANK = "bank"              # 监管银行：回调到账/划款结果
ROLE_AUDITOR = "auditor"        # 审计：只读查看全部台账与轨迹
ROLE_BUYER = "buyer"            # 购房人：仅查看本人合同资金
ROLES = (ROLE_ADMIN, ROLE_DEVELOPER, ROLE_REGULATOR, ROLE_BANK, ROLE_AUDITOR, ROLE_BUYER)

# ---- 请求/结果 ------------------------------------------------------------

@dataclass(frozen=True)
class Request:
    actor: str
    action: str
    payload: dict[str, Any]
    request_id: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Result:
    accepted: bool
    state: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)


def rejected(message: str, state: str = "rejected", data: dict[str, Any] | None = None) -> Result:
    return Result(False, state, message, data or {})


def ok(message: str, state: str = "accepted", data: dict[str, Any] | None = None) -> Result:
    return Result(True, state, message, data or {})


def validate_request(request: Request) -> None:
    if not request.actor or not request.action or not request.request_id:
        raise ValueError("请求缺少身份、动作或幂等键")
    if not isinstance(request.payload, dict):
        raise TypeError("请求数据必须是对象")


def require_amount(payload: dict[str, Any], *names: str) -> list[int]:
    out: list[int] = []
    for name in names:
        value = payload.get(name)
        if isinstance(value, bool):
            raise ValueError(f"{name} 必须是金额整数（分）")
        if isinstance(value, int):
            amount = value
        elif isinstance(value, str) and value.strip().isdigit():
            amount = int(value.strip())
        else:
            raise ValueError(f"{name} 必须是金额整数（分）")
        if amount <= 0:
            raise ValueError(f"{name} 必须为正数")
        out.append(amount)
    return out
