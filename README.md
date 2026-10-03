# 预售资金监管后端

把 **项目 — 监管账户 — 房源 — 合同 — 资金** 关联到同一套状态机，替代定金、施工节点、
解冻申请各自为政的人工台账。只依赖 Python 标准库，数据落在调用方指定的 SQLite 文件中。

## 监管规则如何落地

| 监管要求 | 实现方式 |
| --- | --- |
| 定金不得超过总价 3% | 网签时按备案总价计算上限，超额直接拒绝（`deposit_cap_exceeded`） |
| 节点必须按顺序 | 先「主体封顶」后「竣工验收」，跳序核定拒绝（`node_out_of_order`） |
| 不得提前放款 | 节点未核定 → 不能申请；未审批 → 银行不能划款；合同进入终态 → 待审批驳回、已批准未划款的在银行回调时拦截 |
| 按节点比例解冻 | 封顶累计可解冻 60%，验收 100%；待审批/已批准/已划款均占用额度，驳回后释放 |
| 取消 / 买方违约 / 开发商违约 / 逾期 | 监管人员登记后一次性生成**确定性结算**：买方违约仅没收在账定金，其余三种监管账户内资金全额退购房人；已拨付部分不追回、不重复退 |
| 银行回调重送不重复划款 | `(event_type, bank_serial)` 唯一约束 + `request_id` 幂等表，双重去重 |
| 角色只能处理职责范围内数据 | 6 种角色 + 项目维度绑定；购房人只能看本人合同；跨项目操作返回 `out_of_scope` |
| 事后还原资金依据 | `ledger_entries` 只追加不修改，`fund_trace` 按合同给出逐笔台账与「在账/拨付/退还/没收」汇总 |

金额一律以**分**为单位的整数存储；台账（`ledger_entries`）是资金去向的唯一事实来源，
出账时逐费用桶（定金/首付/按揭，先进先出）校验，监管账户不得透支。

## 角色与动作

- `admin`：`create_user` / `create_project` / `create_account` / `create_unit`
- `developer`（绑定项目）：`create_unit`、`sign_contract`、`request_release`
- `regulator`（绑定项目）：`attest_node`、`approve_release` / `reject_release`、
  `register_event`（`cancelled` / `buyer_breach` / `developer_breach` / `overdue`）
- `bank`（绑定项目）：`bank_inbound`（入账回调）、`bank_outbound_result`（划款结果回调）
- `auditor`：只读 `project_overview` / `contract_detail` / `fund_trace`
- `buyer`（绑定购房人编号）：只读本人 `contract_detail` / `fund_trace`

首个 admin 用户可在用户表为空时自举创建（用户名须与请求 actor 一致）。

## 运行

请求 JSON 经 stdin 传入，`--db` 或环境变量 `ESCROW_DB` 指定数据库：

```bash
echo '{"actor":"admin","action":"create_project","request_id":"r1",
       "payload":{"project_id":"P1","name":"春风里"}}' \
  | python3 -m app.api --db escrow.db
```

典型主线（每个 JSON 字段见下方测试用例）：

```
create_project → create_account → create_user → create_unit → sign_contract
→ bank_inbound(定金/首付/按揭)
→ attest_node(floor_topout) → request_release → approve_release
→ bank_outbound_result(success=true)
→ attest_node(acceptance) → …（额度放宽至 100%）
→ register_event(取消/违约/逾期) → bank_outbound_result(退款/没收)
→ fund_trace（还原每一分钱的去向）
```

## 测试

```bash
python3 -m unittest discover -s tests   # 24 个行为测试
python3 -m compileall app               # 构建检查
```

测试覆盖：3% 定金上限与金额勾稽、节点顺序与额度拦截、FIFO 分桶拨付、
入账/划款回调重送去重、划款失败资金留账、四类终态结算（含已拨付后只退在账余额）、
终态后拦截已批准未划款、角色与项目范围隔离、资金全链路勾稽（入=出+在账）。

## 目录

- `app/contracts.py`：请求/结果结构、状态与监管比例常量
- `app/store.py`：SQLite 表结构、幂等结果、银行事件去重、只追加台账与余额事务
- `app/service.py`：角色鉴权、签约、节点、解冻、结算、回调去重、资金轨迹状态机
- `app/api.py`：本地 stdin/JSON 调用入口
- `tests/test_service.py`：行为测试
