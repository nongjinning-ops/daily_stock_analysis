# 长期交易追踪与复盘系统规划

> 基于 `daily_stock_analysis` 现有基础设施构建，覆盖港股（HK）+ 美股（US）  
> 目标：通过数据驱动的实盘记录、复盘与策略归因，持续提升交易决策质量  
> 状态：规划 v0.2 — 已确认，待进入 Phase 1 开发

---

## 一、系统目标

| 目标 | 说明 |
|------|------|
| **实盘记录** | 精确记录每笔交易（买入/卖出/加仓/减仓），含价格、数量、佣金、市场情绪注释 |
| **策略验证** | 将 AI 给出的分析建议与实际走势对比，量化建议准确率 |
| **复盘日志** | 每次操作后沉淀决策依据、情绪状态、实际结果，形成可检索的交易日记 |
| **持仓监控** | 实时/定时更新当前持仓盈亏，触达关键位提醒 |

---

## 二、与现有系统的关系

```
daily_stock_analysis（现有）
├── data_provider/        ← 行情数据源（复用）
├── src/analyzer.py       ← AI 分析建议（复用，关联引用）
├── src/stock_analyzer.py ← 技术指标（复用）
├── src/notification.py   ← 持仓提醒推送（复用扩展）
├── src/repositories/     ← DB 层（扩展新表）
├── api/v1/               ← FastAPI 路由（后期扩展新端点）
└── ★ 新增: trading/      ← 本系统核心模块
    ├── models.py         ← 数据模型
    ├── repository.py     ← 数据访问层
    ├── service.py        ← 业务逻辑
    ├── cli.py            ← CLI 入口
    └── review.py         ← 复盘引擎
```

**关键集成点**：
- `analysis_history` 表已存储 AI 分析快照（含 ideal_buy / stop_loss / take_profit），新系统通过 `analysis_id` 外键关联
- `stock_daily` 表已有 OHLC 数据，可直接查询入场/出场日的市场数据
- `notification.py` 可复用推送持仓提醒

---

## 三、数据库设计

### 3.1 `trade_records` — 实盘交易记录

```sql
CREATE TABLE trade_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        TEXT UNIQUE NOT NULL,     -- 全局唯一 ID，如 T20260411-001
    stock_code      TEXT NOT NULL,            -- 如 hk00700 / AAPL
    market          TEXT NOT NULL,            -- HK / US
    direction       TEXT NOT NULL,            -- buy / sell / add / reduce
    price           REAL NOT NULL,            -- 实际成交价
    quantity        INTEGER NOT NULL,         -- 股数/手数
    amount          REAL NOT NULL,            -- price * quantity
    commission      REAL DEFAULT 0,           -- 总交易成本（佣金+平台费+印花税+交收费+其他征费）
    net_amount      REAL NOT NULL,            -- 实际资金变动（扣费后）
    trade_date      TEXT NOT NULL,            -- 交易日 YYYY-MM-DD
    trade_time      TEXT,                     -- 成交时间 HH:MM（可选）
    currency        TEXT DEFAULT 'HKD',       -- HKD / USD
    fx_rate_cny     REAL DEFAULT 1.0,           -- 交易时汇率（HKD/USD → CNY），用于历史盈亏换算
    security_type   TEXT DEFAULT 'stock',       -- stock / etf / warrant / bond
    analysis_id     INTEGER,                  -- 关联 analysis_history.id（可为空）
    strategy_tag    TEXT,                     -- 对应策略标签，如 ma_golden_cross
    emotion_score   INTEGER,                  -- 情绪评分 1-5（1极度恐慌 5极度贪婪）
    confidence      INTEGER,                  -- 入场信心 1-5
    reason          TEXT,                     -- 买入/卖出理由（自由文本）
    tags            TEXT,                     -- 逗号分隔标签，如 "趋势跟随,突破"
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);
```

### 3.2 `positions` — 当前持仓快照

```sql
CREATE TABLE positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code      TEXT NOT NULL UNIQUE,
    market          TEXT NOT NULL,
    avg_cost        REAL NOT NULL,            -- 持仓均价（加权平均）
    quantity        INTEGER NOT NULL,         -- 当前持股数
    total_cost      REAL NOT NULL,            -- 总成本（含佣金）
    currency        TEXT DEFAULT 'HKD',
    first_buy_date  TEXT,                     -- 首次建仓日期
    last_update     TEXT DEFAULT (datetime('now')),
    status          TEXT DEFAULT 'open'       -- open / closed
);
```

### 3.3 `trade_reviews` — 复盘记录

```sql
CREATE TABLE trade_reviews (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id       TEXT UNIQUE NOT NULL,     -- R20260411-001
    trade_id        TEXT,                     -- 关联 trade_records.trade_id（可关联一组）
    stock_code      TEXT NOT NULL,
    review_date     TEXT NOT NULL,            -- 复盘日期
    review_type     TEXT NOT NULL,            -- trade / weekly / monthly / position_close
    
    -- 财务维度
    entry_price     REAL,
    exit_price      REAL,
    return_pct      REAL,                     -- 收益率 %
    max_drawdown    REAL,                     -- 持仓期间最大回撤 %
    holding_days    INTEGER,
    
    -- 决策质量维度
    ai_suggestion   TEXT,                     -- 当时 AI 建议（buy/hold/sell）
    ai_target_price REAL,                     -- AI 目标价
    actual_outcome  TEXT,                     -- hit_target / stop_loss / manual_exit / still_holding
    decision_score  INTEGER,                  -- 决策质量自评 1-5
    followed_ai     BOOLEAN,                  -- 是否遵循了 AI 建议
    deviation_reason TEXT,                   -- 若偏离 AI，原因是什么
    
    -- 情绪/心理维度
    pre_emotion     TEXT,                     -- 操作前情绪描述
    post_emotion    TEXT,                     -- 操作后情绪复盘
    bias_detected   TEXT,                     -- 检测到的认知偏差，如 "锚定效应,过度自信"
    
    -- 策略归因维度
    strategy_worked BOOLEAN,                  -- 策略信号是否有效
    signal_quality  INTEGER,                  -- 信号质量 1-5
    market_context  TEXT,                     -- 市场环境描述（趋势市/震荡/极端行情）
    lessons         TEXT,                     -- 本次复盘核心教训
    
    created_at      TEXT DEFAULT (datetime('now'))
);
```

### 3.4 `portfolio_snapshots` — 组合快照（定期归档）

```sql
CREATE TABLE portfolio_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date   TEXT NOT NULL,            -- 快照日期
    total_cost      REAL,                     -- 总成本
    market_value    REAL,                     -- 总市值（按快照日收盘价）
    unrealized_pnl  REAL,                     -- 未实现盈亏
    realized_pnl    REAL,                     -- 累计已实现盈亏
    cash_balance    REAL,                     -- 账户现金
    positions_json  TEXT,                     -- 各持仓明细 JSON
    note            TEXT                      -- 市场环境备注
);
```

---

## 三补充、佣金计算规则（已确认）

### 港股 — 富途证券标准（股票/ETF）

每笔交易总费用 = 佣金 + 平台使用费 + 交收费 + 印花税 + 交易费 + 证监会征费 + 财汇局征费

| 费用项目 | 费率 | 最低/说明 |
|---------|------|----------|
| 佣金 | 0.03% × 成交金额 | 最低 3 HKD/笔 |
| 平台使用费 | 固定 15 HKD/笔 | 普通用户（月单量<35笔） |
| 交收费 | 0.0042% × 成交金额 | 香港结算所代收 |
| 印花税 | 0.10% × 成交金额 | **ETF 免印花税**；不足1港元按1港元计 |
| 交易费 | 0.00565% × 成交金额 | 最低 0.01 HKD |
| 证监会征费 | 0.0027% × 成交金额 | 最低 0.01 HKD |
| 财汇局征费 | 0.00015% × 成交金额 | — |

**示例**（100,000 HKD 普通股票买入）：  
`30 + 15 + 4.2 + 100 + 5.65 + 2.7 + 0.15 = 157.7 HKD`

**ETF 示例**（100,000 HKD ETF 买入，免印花税）：  
`30 + 15 + 4.2 + 0 + 5.65 + 2.7 + 0.15 = 57.7 HKD`

```python
# 港股佣金计算函数（trading/commission.py）
def calc_hk_commission(amount: float, security_type: str = "stock") -> float:
    """Calculate total HK trade commission (Futu standard)."""
    commission = max(amount * 0.0003, 3.0)          # 佣金 0.03%，最低3港元
    platform_fee = 15.0                              # 平台使用费（固定式）
    settlement_fee = amount * 0.000042               # 交收费 0.0042%
    stamp_duty = amount * 0.001 if security_type == "stock" else 0.0  # ETF免印花税
    trading_fee = max(amount * 0.0000565, 0.01)      # 交易费 0.00565%
    sfc_levy = max(amount * 0.000027, 0.01)          # 证监会征费 0.0027%
    frc_levy = amount * 0.0000015                    # 财汇局征费 0.00015%
    return round(commission + platform_fee + settlement_fee +
                 stamp_duty + trading_fee + sfc_levy + frc_levy, 2)
```

### 美股 — 富途证券标准

| 费用项目 | 费率 | 最低/说明 |
|---------|------|----------|
| 佣金 | 0.0049 USD/股 | 最低 0.99 USD，最高成交额 0.5% |
| 平台使用费 | 0.0049 USD/股 | 与佣金相同，另收 |
| SEC 费 | 卖出：0.0000278 × 成交金额 | 仅卖出时收取 |
| 交易活动费 | 卖出：0.000166 USD/股 | 最低 0.01 USD |

```python
def calc_us_commission(price: float, quantity: int, direction: str) -> float:
    """Calculate total US trade commission (Futu standard)."""
    amount = price * quantity
    commission = max(min(quantity * 0.0049, amount * 0.005), 0.99)
    platform_fee = max(min(quantity * 0.0049, amount * 0.005), 0.99)
    sec_fee = amount * 0.0000278 if direction == "sell" else 0.0
    taf = max(quantity * 0.000166, 0.01) if direction == "sell" else 0.0
    return round(commission + platform_fee + sec_fee + taf, 4)
```

### 货币换算（统一 CNY）

- **存储**：`trade_records` 保存原币种金额 + 交易时汇率（`fx_rate_cny`）
- **展示**：统计、报表、持仓盈亏统一换算 CNY
- **汇率来源**：调用 `data_provider` 获取实时汇率（akshare 提供人民币汇率）；历史交易导入时需手动指定或批量填充
- **约定汇率字段**：1 HKD = `fx_rate_cny` CNY；1 USD = `fx_rate_cny` CNY

---

## 四、核心模块设计

### 4.1 CLI 命令设计（Phase 1 交付）

入口：`python -m trading.cli` 或 `python main.py trade <cmd>`

```
交易记录
  trade buy   <code> <price> <qty> [--type stock|etf] [--time HH:MM] [--reason "..."] [--tags "..."]  # 记录买入（自动计算佣金）
  trade sell  <code> <price> <qty> [--type stock|etf]                                              # 记录卖出（自动计算盈亏+佣金）
  trade add   <code> <price> <qty>                                                                  # 加仓（更新均价）
  trade import <csv_file> [--broker futu]                                                           # 从富途导出 CSV 批量导入历史
  trade list  [--code <code>] [--date <YYYY-MM>] [--market HK|US]                                  # 查看交易历史

持仓管理
  position list                       # 查看当前持仓（含实时价格、CNY盈亏）
  position snapshot                   # 生成今日持仓快照（写入 portfolio_snapshots）

复盘
  review new  <trade_id|stock_code>   # 新建复盘（交互式引导）
  review list [--month <YYYY-MM>]     # 历史复盘列表
  review show <review_id>             # 查看复盘详情

统计
  stats summary [--period month|year] [--cny]  # 财务指标汇总（默认 CNY）
  stats win-rate [--strategy <tag>]            # 按策略统计胜率
  stats emotion                                # 情绪-收益相关性分析
  stats ai-accuracy                            # AI 建议准确率统计
```

**交互式复盘示例**（`review new T20260411-001`）：
```
📊 复盘向导: hk09988 (阿里巴巴) 买入 @ 82.5
----------------------------------------
[1/5] 决策质量: 是否遵循了 AI 分析建议? [Y/n]
[2/5] 入场原因（一句话）: > 突破前高，量能配合
[3/5] 情绪评分 (1-5, 1=极度恐慌): > 3
[4/5] 入场信心 (1-5): > 4
[5/5] 标签（逗号分隔，如 趋势跟随,突破）: > 突破,量价配合
----------------------------------------
✅ 复盘记录已保存: R20260411-001
```

### 4.2 复盘引擎（`review.py`）

核心功能：
- **自动关联**：根据 `stock_code` + `trade_date` 查找最近一次 AI 分析（`analysis_history`），自动填充 AI 建议字段
- **价格查询**：出场时自动计算持仓期间最大回撤（查 `stock_daily` 表）
- **偏差检测**：对比 `emotion_score`、`decision_score` 历史分布，标记异常（如情绪分≥4时亏损率）
- **周期统计**：每周/月自动生成统计摘要，可选推送到 notification 渠道

### 4.3 统计指标

| 维度 | 指标 |
|------|------|
| **财务** | 总收益率、胜率、盈亏比、平均持仓天数、最大单笔亏损、最大连续亏损次数、最大回撤 |
| **决策质量** | AI 建议准确率（方向）、AI 目标价命中率、遵循 AI 建议时的胜率 vs 偏离时的胜率 |
| **情绪归因** | 情绪评分分布、高情绪(<2 or >4)入场的胜率、恐慌/贪婪决策失误率 |
| **策略归因** | 各 strategy_tag 的胜率、平均收益率、最佳/最差策略 |

---

## 五、分阶段交付计划

### Phase 1 — CLI 核心（最小可用）

**目标**：能记录交易、看持仓盈亏、做基础复盘

- [ ] 数据库 schema 迁移（4张新表 + Alembic 或 init_db 脚本）
- [ ] `trading/commission.py` — 港股/美股（富途标准）佣金计算
- [ ] `trading/models.py` — SQLAlchemy ORM 模型（含 fx_rate_cny、security_type）
- [ ] `trading/repository.py` — CRUD 数据访问
- [ ] `trading/service.py` — 持仓均价计算、CNY 盈亏计算、汇率获取
- [ ] `trading/cli.py` — `trade`, `position`, `review`, `stats` 命令
- [ ] `trade import` — 富途 CSV 历史导入（ETF/正股均支持）
- [ ] 与 `analysis_history` 的自动关联

**验收标准**：
- 能完整记录一笔港股/美股交易
- `position list` 能显示当前盈亏（调用实时报价）
- `review new` 交互式引导能保存一条完整复盘

### Phase 2 — 统计与洞察

**目标**：数据驱动的复盘，量化自己的弱点

- [ ] `stats` 命令全部指标实现（Sharpe、盈亏比、最大连亏）
- [ ] AI 建议准确率统计（对接 `analysis_history`）
- [ ] 情绪-收益相关性分析（情绪评分 vs 实际收益散点）
- [ ] 周报/月报自动生成（Markdown 格式）
- [ ] 周报推送到**企业微信机器人**（复用 `notification.py` 中 `WECHAT_WEBHOOK_URL`）

### Phase 3 — Web UI 集成（后续规划）

**目标**：可视化复盘，历史回溯

- [ ] FastAPI 新增 `/api/v1/trading/` 端点
- [ ] React 前端：交易日历视图、权益曲线图、胜率热力图
- [ ] 持仓面板集成到现有 Web UI

---

## 六、技术约束与设计原则

1. **最小侵入**：新模块放在 `trading/` 目录，不修改现有 `src/` 逻辑
2. **复用优先**：行情数据继续用 `data_provider/`，通知用 `notification.py`，DB 用同一个 SQLite 实例（通过 `DatabaseManager` 扩展）
3. **CLI First**：Phase 1 所有功能通过 CLI 交互完成，不强依赖 Web
4. **数据完整性**：每笔交易必须记录成本和汇率，避免后期对账困难
5. **隐私本地**：所有交易数据存本地 SQLite，不上传任何服务

---

## 七、已确认决策

| 事项 | 决策 |
|------|------|
| 佣金计算 | **自动计算**，按富途标准（港股/美股分别计算，ETF 免印花税） |
| 货币基准 | **统一 CNY**，原币存储 + 汇率字段，展示层换算 |
| 港股持仓单位 | **按股**（非按手），录入和显示均以股为单位 |
| 推送渠道 | **企业微信**（复用现有 `WECHAT_WEBHOOK_URL`） |
| 历史导入 | **支持**，`trade import <csv>` 兼容富途导出格式 |
| 交易对象 | **股票 + ETF**（security_type 字段区分，ETF 免印花税） |

---

*文档状态：已确认 → 进入 Phase 1 开发*  
*最后更新：2026-04-11*
