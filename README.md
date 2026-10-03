# 预售资金监管后端

一套可运行的住宅项目预售资金监管后端。它把**项目、房源、合同、监管账户**关联为一套统一台账，
取代"定金、施工节点、解冻申请各记一本人工账"的做法，使每笔购房资金的状态在任何时刻只有一个事实来源。

## 解决的核心问题

1. **单一事实来源**：定金/尾款收款计划、节点事件、解冻指令、分类账全部挂在合同与监管账户上，状态不可能互相矛盾。
2. **定金硬约束**：网签时校验定金不得超过总价的 **3%**（整数分精确比较），超额直接拒绝。
3. **事件按顺序驱动确定结果**：只有 `主体封顶 → 竣工验收` 按序确认后才生成解冻指令；违约、取消、逾期各自产生确定的收款/解冻/退款结果。
4. **监管账户是唯一闸门，杜绝提前放款**：节点确认只生成 `pending` 出金指令，银行实际结算前监管账户余额一分不动；终止事件会自动撤销尚未拨付的指令。
5. **银行回调重送绝不重复划款**：三层幂等——请求 `request_id`、回调 `callback_id`、指令 `idem_key`（业务自然键）；同一期款项换回调编号重送也只入一次账。
6. **按角色限定数据范围**：监管人员 / 项目公司 / 监管银行 / 购房人四类角色，只能查看和处理职责范围内的数据。
7. **完整资金追溯**：`fund_trail` 还原每笔购房人资金"入账回单 → 节点/违约事件依据（确认人、时间）→ 出金指令 → 银行回单 → 分类账"的完整证据链，并自动勾稽对账。

## 技术形态

- 纯 Python 标准库，无第三方依赖；持久化为单个 SQLite 文件，金额一律以整数**分**存储。
- 两种入口：HTTP JSON-RPC 服务（Bearer token 鉴权）与本地 CLI（stdin/stdout）。

## 目录

| 路径 | 说明 |
|---|---|
| `app/contracts.py` | 角色、状态、事件、指令类型与错误类型 |
| `app/store.py` | SQLite schema、事务、金额换算 |
| `app/service.py` | 领域状态机、资金规则、RBAC、幂等、资金追溯 |
| `app/api.py` | HTTP 服务（`python3 -m app.api`） |
| `app/cli.py` | 本地调用入口（`python3 -m app.cli`） |
| `tests/` | 43 个行为测试（规则、幂等、RBAC、追溯、持久化、HTTP） |

## 测试

```bash
python3 -m unittest discover -s tests
python3 -m compileall app tests
```

## 启动 HTTP 服务

```bash
python3 -m app.api --db escrow.db --host 127.0.0.1 --port 8080
```

首次启动自动创建内置监管账号 `regulator`，其 API token 只打印一次。

```bash
curl -X POST http://127.0.0.1:8080/ \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"request_id":"r1","action":"register_developer",
       "payload":{"id":"d1","name":"甲地产"}}'
```

所有业务调用统一形如 `{"request_id","action","payload"}`；同一 `request_id` 重放返回首次结果。

## 业务动作（action）

| 动作 | 允许角色 | 说明 |
|---|---|---|
| `register_developer` / `register_bank` | 监管 | 登记项目公司、监管银行 |
| `register_actor` | 监管 | 登记账号并绑定角色与数据范围 |
| `issue_token` / `revoke_token` | 监管 | 签发/吊销 API token（库存 SHA-256 散列） |
| `create_project` | 监管 | 创建项目（公司+银行+监管账户的关联起点） |
| `open_account` | 监管/本行 | 开立项目监管账户 |
| `add_unit` | 监管/本公司 | 房源备案定价 |
| `register_buyer` | 监管/公司 | 登记购房人 |
| `sign_contract` | 监管/本公司 | 网签；校验 3% 定金上限，生成收款计划（定金+尾款） |
| `bank_callback` | 本行 | 入账回单 / 出金结算回单；回调去重 |
| `record_event` | 见下 | 确认节点或异常事件，产生确定资金指令 |
| `get_contract` / `get_project` / `get_account` | 范围内角色 | 查询 |
| `list_instructions` | 各角色按范围 | 银行看待处理出金指令等 |
| `fund_trail` | 范围内角色 | 单笔合同资金全链路追溯与对账 |

### 事件 → 资金结果（`record_event`）

| 事件 | 前置状态 | 确认角色 | 确定结果 |
|---|---|---|---|
| `structure_top` 主体封顶 | `active`（定金到账） | 监管 | 按**已缴款项**累计 60% 生成对项目公司的解冻指令（pending） |
| `acceptance` 竣工验收 | `topped_out` | 监管 | 补足解冻至已缴款 100%；结算完成合同 `completed` |
| `overdue` 逾期未验收 | active/topped_out | 监管 | 撤销未拨付指令，监管留存全额退购房人，合同关闭 |
| `developer_breach` 项目公司违约 | 未终止 | 监管 | 留存全额退购房人，另由项目公司**自有资金**支付违约金（不扣监管余额） |
| `cancel_developer` 项目公司取消 | 未终止 | 监管/公司 | 留存全额退购房人 |
| `buyer_breach` / `cancel_buyer` 购房人违约/取消 | 验收前 | 监管/公司 | 定金（违约金，以留存为限）划付项目公司，余款退购房人 |

说明：

- 解冻金额按"已缴款项 × 累计比例 − 已拨付"计算，封顶时只缴了定金就只解冻定金的 60%，不会按总价预拨。
- 已按节点拨付给项目公司的款项，在后续终止时不追回；退款只动用监管账户中该合同的**留存资金**，分类账始终勾稽。
- 出金指令必须由对应监管银行回调 `settled` 才真正划款；`failed` 或被终止事件 `cancelled` 的指令不能再结算。

### 资金追溯示例输出要点

`fund_trail` 返回：`inflows`（每笔入账的银行回单 callback_id/bank_ref）、
`outflows`（每笔出金的去向、金额、所依据的事件 event_id/确认人、银行回单）、
`ledger`（合同维度留存余额流水；购房人看不到账户级总余额）、
`reconciliation`（已入账 = 已划出监管 + 合同留存，自动校验平衡）。

## CLI 用法

```bash
echo '{"actor":"regulator","action":"create_project",
       "payload":{"id":"p1","developer_id":"d1","bank_id":"b1","name":"一号院"},
       "request_id":"r1"}' | python3 -m app.cli --db escrow.db
```

CLI 首次运行会在 stderr 打印内置监管账号的 API token（HTTP 用）；业务账号仍需监管人员通过
`register_actor` + `issue_token` 建立。
