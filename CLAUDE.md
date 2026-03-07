# CLAUDE.md — GRVT-Lighter 跨交易所套利系统

> Claude Code 每次启动自动读取。所有 AI 编程行为遵循以下规范。

---

## [项目专属区域]

### 项目名称
GRVT-Lighter Cross-Exchange Arbitrage Bot

### 一句话描述
GRVT 与 Lighter 两个永续合约交易所之间的自动化套利系统，支持价差套利、均值回归、Funding Rate 套利三种策略。

### 技术栈
- **语言**: Python 3.11+
- **异步框架**: asyncio
- **交易所连接**: GRVT (ccxt pro WebSocket), Lighter (自有 Python SDK)
- **数据**: Decimal 精度运算, CSV 日志, deque 滚动窗口
- **监控**: Rich 终端 Dashboard, Telegram 通知
- **部署**: 本地运行 / 远程 VPS, `.env` 配置

### 项目结构
```
lighter_grvt/
├── arbitrage.py              # 入口: CLI → 策略分发 → 主循环 (~98行)
├── config.py                 # 配置: dataclass + argparse (~222行)
├── .env                      # 密钥 (不提交)
├── requirements.txt          # 依赖
├── exchanges/
│   ├── base.py               # 抽象基类 (~34行)
│   ├── grvt_client.py        # GRVT 交易所客户端 (~768行)
│   └── lighter_client.py     # Lighter 交易所客户端 (~864行)
├── strategy/
│   ├── arb_strategy.py       # 价差套利策略 (核心) (~513行)
│   ├── order_manager.py      # 订单执行引擎 (GRVT maker + Lighter taker) (~933行)
│   ├── spread_analyzer.py    # 价差分析 + 方向过滤器 (~294行)
│   ├── position_tracker.py   # 本地仓位跟踪 (~110行)
│   ├── order_book_manager.py # 订单簿管理 (~31行)
│   ├── mean_reversion.py     # 均值回归策略 (~995行)
│   └── funding_arb.py        # Funding Rate 套利策略 (~918行)
├── helpers/
│   ├── dashboard.py          # Rich 终端 Dashboard (~500行)
│   ├── logger.py             # 日志配置 (~95行)
│   └── telegram.py           # Telegram 通知 (~99行)
└── data/                     # CSV 数据日志输出目录
```

### 当前阶段
- 三个策略已实现 (arb / mean_reversion / funding_rate)
- 方向过滤器已加入 (positive-rate + neutral zone)
- 数据分析已完成 → 推荐参数已确定
- 下一步: 实盘验证新参数效果

---

## 开发者背景

我不是专业开发者，使用 Claude Code 辅助编程。请：
- 代码加中文注释，关键逻辑额外解释
- 遇到复杂问题先给方案让我确认，不要直接大改
- 报错时解释原因 + 修复方案，不要只贴代码
- 优先最简实现，不要过度工程化

---

## 项目核心规则 (MUST FOLLOW)

### 1. 交易所客户端安全规则
- **API 失败不返回 0，必须 raise** — 返回 0 会导致上层误判为"无仓位"
- **仓位 size 绝不能用 abs()** — sign = 方向信息，去掉就丢失多空
- **平仓必须用 reduce_only=True** — 这是防止平仓变开仓的最后安全网
- **filled_qty 必须封顶 order_quantity** — 不可违反的物理约束
- **pre/post 仓位快照必须来自同一数据源 (REST API)** — 混用本地 tracker 和 API 会引入累积偏差
- **撤单用 cancel_all_orders 不用 cancel_order** — 防幽灵订单
- **WS fill event 不可靠** — GRVT 和 Lighter 的 WS 都不推送 fill，必须 REST 仓位快照兜底

### 2. GRVT 特有约束
- **价格保护带 (error 7201)**: 限价单不能超过 mark price ±2-3%，close_position slippage 上限 2%
- **价格必须对齐 tick_size**: sell 用 ROUND_DOWN, buy 用 ROUND_UP
- **fetch_positions 有延迟**: 下单后立即查可能返空，需重试 5 次 + 1s sleep
- **cancel_order 是 fire-and-forget**: 发了不等于撤了，必须查确认

### 3. Lighter 特有约束
- **position 有独立 sign 字段**: sign=1(Long)/-1(Short), position=绝对值, 必须乘 sign
- **IOC 必须用 ORDER_TYPE_MARKET(1)**: LIMIT+IOC 全 0 会被当普通限价单
- **cancel_all 用 CANCEL_ALL_TIF_IMMEDIATE(0)**: 不是 ORDER_TIME_IN_FORCE_*!
- **close slippage 上限 2%**: 超过会被 error 21733 拒绝

### 4. Shutdown 安全规则
- **超时 ≥240s**: 覆盖最坏路径 (所有重试 + settle 等待)
- **每轮用交易所 API 实时值**: 不用本地 stale tracker
- **IOC settle 需 8s+ 等待**: POSITION_STATE_ORDER 状态要等

---

## 数据分析结论 (2026-03-07)

### 价差特征
- **方向不对称**: 任意时段只有一个方向有结构性优势，双向交易互相抵消
- **动量型 (非均值回归)**: 自相关 0.56-0.82, 连续 N 正后反转概率递减
- **Lighter 内部 spread ~$12**: 隐性交易成本，阈值必须 > $12

### 推荐参数 (Arb 策略)
```bash
python arbitrage.py --size 0.002 --max-position 0.01 \
  --long-threshold 15 --short-threshold 15 --min-spread 5 \
  --signal-cooldown 10 --persistence-count 8 \
  --warmup-samples 120 --natural-spread-window 600 \
  --direction-filter-window 600 --direction-filter-threshold 0.65
```

---

## 关键路径文件 (Critical Path — 改动必须额外审查)

以下文件直接涉及资金安全，任何改动（含重构）都必须走完整审查流程：

| 文件 | 风险等级 | 原因 |
|------|---------|------|
| `exchanges/grvt_client.py` | **P0** | 下单/撤单/仓位查询，错误直接导致资金损失 |
| `exchanges/lighter_client.py` | **P0** | 同上 |
| `strategy/order_manager.py` | **P0** | 订单执行引擎，幽灵订单/过量对冲/成交检测 |
| `strategy/arb_strategy.py` | **P1** | 信号触发 + shutdown 平仓 |
| `strategy/spread_analyzer.py` | **P1** | 信号门控逻辑，错误导致误触发 |
| `config.py` (validate部分) | **P2** | 参数校验，漏检会让非法参数进入策略 |

**琐碎任务绕过规则：** 以下改动可跳过额外审查：
- ≤5 行修改，且**不**涉及上述关键路径文件
- 仅修改日志消息、注释、配置默认值 — 不涉及控制流或状态变更
- 如有模糊地带，宁可多审查

---

## 实施流程 (非琐碎任务)

### Phase 0: 计划 + 辩论（阻塞网关）

**触发条件：** 新功能 / 架构变更 / 涉及关键路径文件的修复
**绕过条件：** 符合上述琐碎任务定义

1. **起草蓝图** — 明确要改什么文件、怎么改、为什么改
2. **内部辩论** — 多个 Sub-Agent 互相审查方案（推荐 3 个 Agent 辩论）
3. **迭代上限 3 轮** — 3 轮辩论仍无法达成共识 → 停下来问用户
4. **计划锁定后才能开始实施** — 辩论期间不写任何实现代码

> 可选：用 Codex Skill 引入跨模型审查（见下方"跨模型审查"节）

### Phase 1: 实施 + 内部审查

1. **编写代码** — 按锁定的蓝图实施
2. **内部审查** — 委派 Sub-Agent 审查，重点关注：
   - 竞争条件（特别是 asyncio 下单环节）
   - 仓位 sign 是否正确
   - filled_qty 是否封顶
   - 新代码是否违反"项目核心规则"
3. **语法验证** — 所有改动文件 `ast.parse()` 通过
4. **如有测试** — 运行并确保全部通过

### Phase 2: 完成摘要

每次完成非琐碎任务，提供结构化汇报：
```
最终状态: [成功 ✅ / 需确认 🛑]
改动文件: (列出)
关键决策: (用简短项目符号列出)
剩余事项: (任何需要用户确认的边缘情况)
```

### 升级协议 — 何时必须停下来问用户

| 触发条件 | 行动 |
|----------|------|
| 辩论 3 轮仍无共识 | 停止，汇报分歧点 |
| 改动涉及交易策略假设 (阈值、费率) | 先确认再改 |
| 对冲/平仓逻辑的架构级变更 | 先确认再改 |
| 不确定是否会破坏现有行为 | 先确认再改 |
| 测试持续失败且无法定位原因 | 停止，汇报现状 |

---

## 跨模型审查 (可选, 推荐)

> 基于论文 "Understanding Agent Scaling in LLM-Based Multi-Agent Systems via Diversity" (arXiv:2602.03794)：
> 同源模型的多 Agent 容易形成信息茧房。引入不同模型审查能显著提升代码质量。

**工具: skill-codex** (GitHub: `skills-directory/skill-codex`)
- 通过 `Skill("codex")` 调用 Codex 对计划或代码进行独立审查
- Plan 阶段用 `xhigh` reasoning → 挑架构缺陷
- 代码审查阶段用 `high` reasoning → 挑实现 bug
- 未安装 Codex 时不阻塞流程，按内部审查流程继续

---

## 上下文管理规范

### 1. 文件行数参考限制

| 文件类型 | 参考上限 | 超限动作 |
|----------|----------|----------|
| 单个源代码文件 | **500 行** | 考虑拆分 (交易所客户端例外) |
| 配置文件 | **300 行** | 拆分为多个配置 |
| 测试文件 | **300 行** | 按功能拆分 |

> 注: 交易所客户端 (~800 行) 因包含大量 REST/WS 兜底逻辑，暂不拆分。

### 2. 定期清理

当我说 **"清理一下"** 时，执行以下检查：
1. 行数审计：列出所有超限文件
2. 死代码检测：未被 import/调用的函数
3. TODO/FIXME 清理
4. CLAUDE.md 项目结构是否与实际一致
5. 依赖检查：requirements.txt 中未使用的库

---

## Sub-Agent 并行调度规则

### 角色分工（非琐碎任务建议分工）

| 角色 | 推荐模型 | 职责 |
|------|---------|------|
| **实现者** (implementer) | Sonnet 4.6 | 按蓝图写代码，不负责修 bug |
| **审查者** (reviewer) | Sonnet 4.6 | 审查代码逻辑、竞争条件、仓位 sign |
| **调试者** (debugger) | Sonnet 4.6 | 修复测试失败、语法错误、运行时 bug |
| **辩论者** (debater) | Sonnet 4.6 ×3 | Phase 0 方案辩论，互相挑刺 |

> 主 CLI (Opus) 负责统筹、仲裁辩论、锁定计划。
> 简单任务可由主 CLI 直接完成，无需分工。

### 并行派遣（所有条件满足时）
- 3+ 个不相关任务
- 不操作同一个文件
- 无输入输出依赖

### 顺序派遣（任一条件触发时）
- B 需要 A 的输出
- 操作同一文件
- 范围不明确

### Sub-Agent 调用要求
1. 操作哪些文件（写）
2. 读取哪些文件（只读）
3. 完成标准是什么
4. 不许碰哪些文件

### 研究/分析类任务后台运行，不阻塞主对话

---

## 编码规范

### 错误处理
- 所有外部调用 (API, WS, 文件 IO) 必须 try-except
- **交易所 API 失败 → raise，不返回默认值**
- 日志记录错误详情, 但不向用户暴露堆栈

### 函数设计
- 函数名用动词开头: `get_position()`, `place_order()`, `check_signal()`
- 每个函数有 docstring
- 全程 Decimal 运算 (金融精度)

### 依赖管理
- 不要自行引入新依赖，需要新库先问我
- 优先使用标准库和项目已有依赖
- 新增依赖立即更新 requirements.txt

### 配置管理
- 敏感信息放 `.env`，通过 `os.getenv()` 读取
- 绝不在代码中硬编码密钥

---

## Git 规范

### Commit 信息格式
```
<类型>: <一句话描述>

类型: feat | fix | refactor | docs | chore
```

### 每次 commit 前
- 确认没有提交 .env / __pycache__ / .cache/
- 确认 `python -c "import ast; ast.parse(open('file.py').read())"` 通过

---

## 沟通规范

### 当 AI 不确定时
- 直接说不确定，给 2-3 个方案让我选
- 标明每个方案的优缺点

### 当任务太大时
- 先给出拆分计划 (5-8 步)，让我确认后再执行
- 每完成一步告诉我进度

### 当代码出问题时
- 先说是什么问题（一句话）
- 再说为什么出了这个问题（原因分析）
- 最后给修复方案

### 关键词触发

| 我说 | 你做 |
|------|------|
| "清理一下" | 执行定期清理流程 |
| "拆一下" | 检查指定文件行数，给出拆分方案 |
| "健康检查" | 运行完整项目健康度检查 |
| "现在到哪了" | 总结当前进度 |
| "省着点" | 减少 token：回复更简短 |
| "全力跑" | 可以并行、大改、不用每步确认 |
| "从第0阶段开始" | 强制走完整 Phase 0 → 1 → 2 流程 |
| "重新加载" | 重新读取 CLAUDE.md 恢复上下文 |

---

## 性能优化规范

### Token 节省
1. 修改文件只输出变更部分
2. 长文件只输出相关函数
3. 使用 `// ... existing code ...` 标记未修改部分

### 上下文保鲜
1. 对话超 20 轮后建议 `/compact`
2. 切换模块时建议开新 session
3. 大量探索用 sub-agent
4. Debug 用 Explore sub-agent
