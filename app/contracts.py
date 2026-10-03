"""预售资金监管的输入输出约定与领域常量。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ---- 角色 -----------------------------------------------------------------
ROLE_REGULATOR = "regulator"      # 监管人员：全部项目只读 + 关键事件确认/审批
ROLE_DEVELOPER = "developer"      # 项目公司：仅本公司项目，可发起申请
ROLE_BANK = "bank"                # 监管银行：账户出入金回调、指令查询
ROLE_BUYER = "buyer"              # 购房人：仅本人合同
ROLES = (ROLE_REGULATOR, ROLE_DEVELOPER, ROLE_BANK, ROLE_BUYER)

# ---- 合同状态 -------------------------------------------------------------
CONTRACT_DRAFT = "draft"
CONTRACT_ACTIVE = "active"              # 已备案，资金进入监管
CONTRACT_STRUCTURE_TOPPED_OUT = "topped_out"
CONTRACT_ACCEPTED = "accepted"          # 已验收（节点全部完成）
CONTRACT_CANCELLED_DEVELOPER = "cancelled_developer"   # 项目公司违约/取消
CONTRACT_CANCELLED_BUYER = "cancelled_buyer"           # 购房人违约/取消
CONTRACT_OVERDUE_CLOSED = "overdue_closed"             # 逾期未验收，退款关闭
CONTRACT_COMPLETED = "completed"                       # 验收完成且资金全额拨付
TERMINAL_STATES = {
    CONTRACT_CANCELLED_DEVELOPER,
    CONTRACT_CANCELLED_BUYER,
    CONTRACT_OVERDUE_CLOSED,
    CONTRACT_COMPLETED,
}

# ---- 节点事件类型（必须按登记/确认顺序推进） ------------------------------
EVENT_STRUCTURE_TOP = "structure_top"       # 主体封顶
EVENT_ACCEPTANCE = "acceptance"             # 竣工验收
EVENT_OVERDUE = "overdue"                   # 逾期未验收
EVENT_DEVELOPER_BREACH = "developer_breach"  # 项目公司违约
EVENT_BUYER_BREACH = "buyer_breach"          # 购房人违约
EVENT_CANCEL_DEVELOPER = "cancel_developer"  # 项目公司取消
EVENT_CANCEL_BUYER = "cancel_buyer"          # 购房人取消

NODE_EVENTS = (EVENT_STRUCTURE_TOP, EVENT_ACCEPTANCE)

# 节点 -> 完成后允许向项目公司解冻的累计比例
RELEASE_RATIO = {
    EVENT_STRUCTURE_TOP: 0.60,
    EVENT_ACCEPTANCE: 1.00,
}

# ---- 收款计划条目 ---------------------------------------------------------
SCHEDULE_DEPOSIT = "deposit"   # 定金
SCHEDULE_INSTALLMENT = "installment"  # 尾款/分期
SCHEDULE_TYPES = (SCHEDULE_DEPOSIT, SCHEDULE_INSTALLMENT)

ITEM_PENDING = "pending"
ITEM_PAID = "paid"
ITEM_REFUNDED = "refunded"
ITEM_FORFEITED = "forfeited"
ITEM_DISBURSED = "disbursed"            # 已按节点拨付项目公司
ITEM_PARTIAL_DISBURSED = "partial_disbursed"  # 部分拨付、部分退还

# ---- 出入金指令 -----------------------------------------------------------
FLOW_IN = "in"
FLOW_OUT_DEVELOPER = "out_developer"   # 解冻拨付给项目公司
FLOW_OUT_BUYER = "out_buyer"           # 退购房人
PENALTY_BUYER = "penalty_buyer"        # 购房人违约金（从定金划给项目公司）
PENALTY_DEVELOPER = "penalty_developer"  # 项目公司违约金（监管余额内划给购房人）
FLOW_TYPES = (FLOW_IN, FLOW_OUT_DEVELOPER, FLOW_OUT_BUYER,
              PENALTY_BUYER, PENALTY_DEVELOPER)

INSTR_PENDING = "pending"
INSTR_SETTLED = "settled"
INSTR_FAILED = "failed"
INSTR_CANCELLED = "cancelled"

# 哪些出金类型动用监管账户内资金（项目公司违约金从其自有账户支付，不扣监管余额）
ESCROW_DRAWING_FLOWS = (FLOW_OUT_DEVELOPER, FLOW_OUT_BUYER, PENALTY_BUYER)

# 出金去向
DEST_DEVELOPER = "developer"
DEST_BUYER = "buyer"

DEPOSIT_RATIO_CAP = 0.03  # 定金不得超过总价的 3%


class DomainError(Exception):
    """业务规则被拒绝（不是系统故障）。"""

    def __init__(self, message: str, code: str = "rejected") -> None:
        super().__init__(message)
        self.code = code


class AuthError(DomainError):
    def __init__(self, message: str = "无权执行该操作或访问该数据") -> None:
        super().__init__(message, "forbidden")


class NotFound(DomainError):
    def __init__(self, message: str = "记录不存在") -> None:
        super().__init__(message, "not_found")


class ConflictError(DomainError):
    def __init__(self, message: str = "当前状态不允许该操作") -> None:
        super().__init__(message, "conflict")


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


def validate_request(request: Request) -> None:
    if not request.actor or not request.action or not request.request_id:
        raise ValueError("请求缺少身份、动作或幂等键")
    if not isinstance(request.payload, dict):
        raise TypeError("请求数据必须是对象")
