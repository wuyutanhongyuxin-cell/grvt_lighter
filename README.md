<h1 align="center">
  ⚡ Lighter × GRVT Cross-Exchange Arbitrage Bot
</h1>

<p align="center">
  <b>高频跨交易所永续合约套利系统 — 三策略模式，Maker/Taker 模型，WebSocket 事件驱动</b>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-blue?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Async-asyncio-green" alt="Asyncio">
  <img src="https://img.shields.io/badge/Loop-50ms-orange" alt="50ms Loop">
  <img src="https://img.shields.io/badge/Dashboard-Rich-purple?logo=gnu-bash&logoColor=white" alt="Rich Dashboard">
  <img src="https://img.shields.io/badge/License-MIT-yellow" alt="License">
</p>

---

## 📋 目录 / Table of Contents

- [概述 / Overview](#概述--overview)
- [三策略模式 / Strategy Modes](#三策略模式--strategy-modes)
- [核心特性 / Features](#核心特性--features)
- [终端 Dashboard / Terminal Dashboard](#终端-dashboard--terminal-dashboard)
- [系统架构 / Architecture](#系统架构--architecture)
- [交易流程 / Trade Lifecycle](#交易流程--trade-lifecycle)
- [快速开始 / Quick Start](#快速开始--quick-start)
- [命令行参数 / CLI Reference](#命令行参数--cli-reference)
- [配置说明 / Configuration](#配置说明--configuration)
- [风险管理 / Risk Management](#风险管理--risk-management)
- [数据输出 / Data Output](#数据输出--data-output)
- [推荐参数 / Recommended Parameters](#推荐参数--recommended-parameters)
- [Telegram 通知 / Notifications](#telegram-通知--notifications)
- [项目结构 / Project Structure](#项目结构--project-structure)
- [核心设计决策 / Design Decisions](#核心设计决策--design-decisions)
- [常见问题 / FAQ](#常见问题--faq)
- [免责声明 / Disclaimer](#免责声明--disclaimer)

---

## 概述 / Overview

本系统实现 **[Lighter](https://lighter.xyz)** 与 **[GRVT](https://grvt.io)** 两个去中心化永续合约交易所之间的跨交易所价差套利。

**核心思路**：当同一标的在两个交易所出现价差时，在价格较低的一方买入、价格较高的一方卖出，锁定无风险利润。

```
┌─────────────────────────────────────────────────────────────────┐
│                     套利信号检测                                  │
│                                                                 │
│   Lighter Bid > GRVT Bid + threshold  →  Long GRVT Signal      │
│   GRVT Ask < Lighter Ask - threshold  →  Short GRVT Signal     │
│                                                                 │
│   ┌──────────┐     价差 > 阈值      ┌──────────┐               │
│   │  GRVT    │  ◄─────────────────► │ Lighter  │               │
│   │ (Maker)  │   BUY/SELL post-only │ (Taker)  │               │
│   │ 挂单端   │                      │  吃单端  │               │
│   └──────────┘                      └──────────┘               │
│       ▲ WS fill event                    ▲ WS fill event       │
│       │ 确认成交                          │ 确认成交              │
└───────┼──────────────────────────────────┼─────────────────────┘
        │            Bot (50ms loop)       │
        └──────────────────────────────────┘
```

### 为什么选择这对交易所？

| 特性 | GRVT | Lighter |
|------|------|---------|
| 角色 | **Maker（挂单方）** | **Taker（吃单方）** |
| 手续费 | Maker rebate（负费率返佣） | 零手续费 |
| 订单类型 | Post-only 限价单 | FOK/IOC 市价单 |
| 数据源 | Mini Ticker WS | 自定义 Orderbook WS |
| 成交确认 | WS order feed | WS account_orders |

**手续费优势**：GRVT maker rebate + Lighter 零费率 = 几乎零交易成本。

---

## 三策略模式 / Strategy Modes

通过 `--strategy` 参数切换，三种策略共享相同的执行引擎（GRVT maker post-only + Lighter taker IOC）和安全机制：

### 1. 价差套利 `--strategy arb`（默认）

经典跨交易所价差套利。当同一标的在两个交易所出现价差超过阈值时，低买高卖锁定利润。

```bash
python arbitrage.py --strategy arb --ticker BTC --size 0.001 --max-position 0.01 \
    --long-threshold 20 --short-threshold 20
```

### 2. 价差均值回归 `--strategy mean_reversion`

基于价差 z-score 的均值回归策略。计算 `grvt_mid - lighter_mid` 的滚动均值和标准差，当 z-score 偏离均值超过阈值时入场，回归时出场。

```
状态机: FLAT ─── |z| > entry_z ─── POSITIONED ─── |z| < exit_z ─── FLAT
                                        │
                                   |z| > stop_z → 止损出场
```

```bash
python arbitrage.py --strategy mean_reversion --ticker BTC --size 0.001 --max-position 0.01 \
    --mr-entry-z 2.0 --mr-exit-z 0.5 --mr-stop-z 4.0 --mr-window 300 --mr-warmup 60
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `--mr-window` | 300 | 滚动窗口样本数（~5min @1样本/s） |
| `--mr-entry-z` | 2.0 | 入场 z-score 阈值 |
| `--mr-exit-z` | 0.5 | 出场 z-score 阈值（回归判定） |
| `--mr-stop-z` | 4.0 | 止损 z-score 阈值 |
| `--mr-maker-timeout` | 60 | GRVT maker 等待超时（秒） |
| `--mr-warmup` | 60 | 预热样本数（收集统计数据） |

### 3. Funding Rate 套利 `--strategy funding_rate`

捕获两个交易所之间的 funding rate 差异。建立 delta-neutral 持仓（一侧做多一侧做空），通过 funding rate 差额获利。

```bash
python arbitrage.py --strategy funding_rate --ticker BTC --size 0.001 --max-position 0.01 \
    --fr-check-interval 60 --fr-min-diff 0.0001 --fr-hold-min-hours 1
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `--fr-check-interval` | 60 | Rate 检查间隔（秒） |
| `--fr-min-diff` | 0.0001 | 最小 rate 差异阈值（0.01%） |
| `--fr-hold-min-hours` | 1 | 最短持仓时间（小时），防频繁翻转 |

---

## 核心特性 / Features

### 交易引擎
- **50ms 主循环** — 亚秒级价差捕获
- **WS 事件驱动成交确认** — 不依赖 REST 轮询，毫秒级感知
- **5 阶段交易生命周期** — 从预检到确认，每一步都有安全保障
- **Post-only 自动重试** — Maker 订单被拒最多重试 15 次，自动调整价格

### 安全机制
- **仓位双源追踪** — 本地增量 + API 周期对账（60s）
- **仓位偏差紧急停机** — 净暴露超过 `order_qty × 3` 立即停止
- **余额双重确认** — 防止 API 502 错误导致假性低余额触发停机
- **退出强制平仓** — 3 轮重试 + 2% 滑点封顶（GRVT 价格保护带限制）保证平仓
- **Ctrl+C 不可中断关机** — shutdown 期间屏蔽信号，确保完整退出
- **Lighter fill 超时安全默认** — 未确认成交 = 视为零成交，宁漏不错

### 可观测性
- **Rich 终端 Dashboard** — 全屏实时面板：健康状态、BBO、价差热力图、仓位、交易记录、事件日志
- **双通道日志分离** — Dashboard 模式下日志只走文件（纯文本无 ANSI），终端留给面板
- **Telegram 实时通知** — 启动、交易、心跳、紧急告警
- **CSV 数据记录** — BBO 快照 + 完整交易记录
- **多级日志系统** — 文件 DEBUG 全量记录，Dashboard WARNING+ 事件面板

---

## 终端 Dashboard / Terminal Dashboard

默认启动时 Bot 会显示 Rich 全屏 Dashboard，包含 6 个实时面板：

```
┌─────────────────────────────────────────────────────────────────────┐
│  ⚡ LIGHTER × GRVT  ─  BTC-PERP    ⏱ 1:23:45    📊 42 trades     │
├──────────┬──────────────────────────────┬───────────────────────────┤
│ Health   │  BBO — Best Bid / Ask        │  Spread → Signal          │
│          │                              │                           │
│ GRVT     │      Bid        Ask   Spread │  LONG GRVT                │
│ ● WS     │ GRVT  95001.2   95002.5  1.3 │  ▲ $  18.50 / $20       │
│          │ Ltr   95005.3   95006.1  0.8 │  ━━━━━━━━━━━━━━━╌╌╌      │
│ LIGHTER  │ Cross  Δ +4.1    Δ -3.6      │                           │
│ ● WS     │                              │  SHORT GRVT               │
│ ● OB     │                              │  △ $  16.20 / $20        │
│ ● Acct   │                              │  ━━━━━━━━━━━━━╌╌╌╌╌      │
├──────────┼──────────────────────────────┴───────────────────────────┤
│ Positions│  Recent Trades                                           │
│          │  Time   Dir   GRVT     Lighter  Size   Spread  P&L       │
│ GRVT     │  14:23  LONG  $95001   $95005   0.001  $+4.00  $+0.0040 │
│ +0.0010  │  14:20  SHORT $95010   $95006   0.001  $+4.00  $+0.0040 │
│ Lighter  │  14:18  LONG  $95002   $95006   0.001  $+4.00  $+0.0040 │
│ -0.0010  │                                                          │
│ Net 0.00 │                                                          │
├──────────┴──────────────────────────────────────────────────────────┤
│ Event Log                                                           │
│  14:23:01 ✦ long_grvt 0.001 spread=$4.00 pnl=$0.0040              │
│  14:20:15 ⚠ GRVT WS stale, skipping trading                       │
│  14:18:02 ✦ short_grvt 0.001 spread=$4.00 pnl=$0.0040             │
├─────────────────────────────────────────────────────────────────────┤
│  Ctrl+C exit  │ L=$20 S=$20 │ timeout=5s │ cooldown=2s             │
└─────────────────────────────────────────────────────────────────────┘
```

### 双通道设计

```
┌─────────────┐    ┌──────────────────────┐
│  FileHandler │───▶│ logs/arbitrage_*.log │  纯文本，无 ANSI，可 tail -f
│  (DEBUG+)    │    │ (完整结构化日志)      │
└─────────────┘    └──────────────────────┘

┌─────────────┐    ┌──────────────────────┐
│  Dashboard   │───▶│ Terminal (stdout)    │  Rich 全屏面板
│  (Rich Live) │    │ (实时可视化)          │
└─────────────┘    └──────────────────────┘

┌─────────────┐    ┌──────────────────────┐
│  DataLogger  │───▶│ data/bbo_*.csv       │  CSV 不变
│  (CSV)       │    │ data/trades_*.csv    │
└─────────────┘    └──────────────────────┘
```

- **Dashboard 模式（默认）**：终端显示全屏面板，日志自动写入文件，**不再需要 `tee`**
- **纯日志模式（`--no-dashboard`）**：和之前完全一样，适合调试、后台运行、`tee` 管道

---

## 系统架构 / Architecture

```
arbitrage.py                    ← 入口：信号处理 + 策略分发 + 优雅关机
    │
    ├── config.py               ← 配置：CLI args + .env + 三策略参数
    │
    ├── strategy/
    │   ├── arb_strategy.py     ← 价差套利：50ms 主循环 + 信号检测
    │   ├── mean_reversion.py   ← 均值回归：z-score 信号 + 状态机
    │   ├── funding_arb.py      ← Funding Rate：rate 差异 + delta-neutral
    │   ├── order_manager.py    ← 交易核心：5 阶段生命周期管理（arb 专用）
    │   ├── spread_analyzer.py  ← 价差计算 + 信号触发（arb 专用）
    │   ├── position_tracker.py ← 仓位追踪 + API 对账（共用）
    │   └── order_book_manager.py ← 双交易所 BBO 聚合（共用）
    │
    ├── exchanges/
    │   ├── base.py             ← 交易所抽象基类
    │   ├── grvt_client.py      ← GRVT Maker 端（grvt-pysdk）+ funding rate
    │   └── lighter_client.py   ← Lighter Taker 端（自定义 WS + lighter-sdk）
    │
    └── helpers/
        ├── dashboard.py        ← Rich 全屏 Dashboard（可选）
        ├── logger.py           ← 日志（双模式）+ CSV 数据记录器
        └── telegram.py         ← Telegram 通知
```

### 数据流

```
 ┌─────────────┐  mini.s WS   ┌────────────────┐
 │  GRVT API   │────────────►│  GrvtClient     │──► get_bbo()
 │             │  order WS    │  (Maker Side)   │──► wait_for_fill()
 └─────────────┘────────────►│                  │──► place_post_only_order()
                              └────────┬────────┘
                                       │
                                       ▼
                              ┌────────────────┐
                              │  ArbStrategy    │  50ms loop
                              │                 │  health check → BBO → spread → signal
                              │  SpreadAnalyzer │  diff_long / diff_short > threshold?
                              │  PositionTracker│  risk check, capacity check
                              └────────┬────────┘
                                       │
                                       ▼
                              ┌────────────────┐
 ┌─────────────┐  OB WS      │  OrderManager   │  execute_arb()
 │ Lighter API │────────────►│  (Trade Engine)  │  Phase 0→1→2→3→4→5
 │             │  acct WS     │                  │
 └─────────────┘────────────►│  LighterClient  │──► place_ioc_order()
                              │  (Taker Side)   │──► wait_for_fill()
                              └─────────────────┘
```

---

## 交易流程 / Trade Lifecycle

每笔套利交易严格经过 **6 个阶段**（Phase 0-5），任一阶段失败都有安全回退：

```
Phase 0: Pre-trade Check          Phase 1: GRVT Maker Order
┌────────────────────────┐        ┌────────────────────────┐
│ • 刷新双端仓位 (API)    │        │ • Post-only 限价单      │
│ • 检查仓位净暴露        │   ──►  │ • Buy: ask - 1 tick    │
│ • 检查方向容量          │        │ • Sell: bid + 1 tick   │
│ • 失败 → 跳过本次       │        │ • 被拒 → 重试 (max 15) │
└────────────────────────┘        └──────────┬─────────────┘
                                             │
Phase 2: Wait GRVT Fill                      ▼
┌────────────────────────┐        ┌────────────────────────┐
│ • WS order event 等待   │   ◄──  │ • asyncio.Event 等待   │
│ • Timeout → 取消 + 检查 │        │ • fill_timeout 秒      │
│ • 部分成交 → 继续对冲    │        │ • 超时 → filled_size=0 │
│ • 零成交 → 安全退出      │        │ • 不假设全额成交！      │
└────────────┬───────────┘        └────────────────────────┘
             │
             ▼ confirmed_fill_size (NOT order_quantity!)
Phase 3: Lighter Taker Order      Phase 4: Wait Lighter Fill
┌────────────────────────┐        ┌────────────────────────┐
│ • FOK 市价单            │   ──►  │ • WS account_orders    │
│ • size = GRVT 确认数量   │        │ • 30s timeout          │
│ • 0.2% slippage         │        │ • 超时 → 视为零成交     │
│ • 失败 → 紧急 TG 告警   │        │ • 发送 EMERGENCY 通知   │
└────────────────────────┘        └──────────┬─────────────┘
                                             │
Phase 5: Record & Update                     ▼
┌────────────────────────┐        ┌────────────────────────┐
│ • 更新双端仓位           │   ◄──  │ • 计算捕获价差          │
│ • CSV 记录交易           │        │ • Long: L_price - G_price│
│ • Telegram 通知         │        │ • Short: G_price - L_price│
│ • 统计交易次数           │        │ • 估算利润              │
└────────────────────────┘        └────────────────────────┘
```

### 关键安全规则

| 规则 | 说明 | 来源 |
|------|------|------|
| Lighter size = confirmed GRVT fill | **永远不用** `order_quantity` 作为对冲数量 | 01-lighter 教训 |
| Fill timeout → filled_size = 0 | 超时不假设成交，宁漏不错 | 01-lighter 教训 |
| GRVT fill 不到 → 不开 Lighter 单 | Maker 没确认就不对冲 | 资金安全 |
| Lighter 对冲失败 → EMERGENCY | 单边成交是最危险状态 | 风控核心 |

---

## 快速开始 / Quick Start

### 1. 环境要求

- **Python** 3.11+
- **GRVT 账户** — API Key + Private Key + Trading Account ID
- **Lighter 账户** — API Key Private Key + Account Index
- （可选）**Telegram Bot** — 用于实时通知

### 2. 安装依赖

```bash
git clone https://github.com/wuyutanhongyuxin-cell/grvt_lighter.git
cd grvt_lighter
pip install -r requirements.txt
```

### 3. 配置环境变量

复制示例配置并填写密钥：

```bash
cp .env.example .env
```

编辑 `.env` 文件：

```env
# GRVT Exchange
GRVT_PRIVATE_KEY=0x你的私钥
GRVT_API_KEY=你的API密钥
GRVT_TRADING_ACCOUNT_ID=你的交易账户ID
GRVT_ENV=prod          # prod 或 testnet

# Lighter Exchange
LIGHTER_API_KEY_PRIVATE_KEY=0x你的私钥
LIGHTER_ACCOUNT_INDEX=0
LIGHTER_API_KEY_INDEX=0

# Telegram（可选）
TG_BOT_TOKEN=你的bot_token
TG_CHAT_ID=你的chat_id
```

### 4. 启动运行

```bash
# Dashboard 模式（默认）— 全屏实时面板
python arbitrage.py --ticker BTC --size 0.001 --max-position 0.01 \
    --long-threshold 20 --short-threshold 20

# ETH 套利，自定义阈值
python arbitrage.py --ticker ETH --size 0.01 --max-position 0.1 \
    --long-threshold 5 --short-threshold 5

# 纯日志模式 — 调试 / 后台运行 / tee 管道
python arbitrage.py --no-dashboard --ticker BTC --size 0.001 --max-position 0.01 \
    --log-level DEBUG

# 纯日志 + tee 保存（适合远程调试）
python arbitrage.py --no-dashboard --ticker BTC --size 0.001 --max-position 0.01 \
    2>&1 | tee -a "logs/run_BTC_$(date +%F_%H%M%S).log"

# 后台运行（推荐生产环境）
nohup python arbitrage.py --no-dashboard --ticker BTC --size 0.001 --max-position 0.01 &
# 日志自动写入 logs/arbitrage_*.log，不需要 tee
```

> **Dashboard vs --no-dashboard**：Dashboard 模式下终端被全屏面板占用，日志只写入文件（`logs/arbitrage_*.log`），可用 `tail -f logs/arbitrage_*.log` 在另一个终端实时查看。`--no-dashboard` 模式日志同时输出到终端和文件，适合 `tee`、`nohup`、`screen` 等场景。

### 5. 停止运行

按 `Ctrl+C` 触发优雅关机：
1. 取消所有挂单
2. 三轮重试平仓（GRVT 2% 滑点封顶 + Lighter 递增滑点）
3. 恢复终端（Dashboard 模式下自动退出全屏）
4. 断开 WebSocket
5. 发送 Telegram 停止通知
6. 关闭数据文件

---

## 命令行参数 / CLI Reference

### 通用参数（三种策略共用）

| 参数 | 必需 | 默认值 | 说明 |
|------|:----:|--------|------|
| `--strategy` | | `arb` | 策略模式：`arb` / `mean_reversion` / `funding_rate` |
| `--ticker` | | `BTC` | 交易标的（`BTC`, `ETH`） |
| `--size` | ✅ | — | 每笔交易数量（基础资产单位） |
| `--max-position` | ✅ | — | 单侧最大持仓量 |
| `--mode` | | `maker_taker` | 执行模式：`maker_taker` / `market_market` |
| `--fill-timeout` | | `1` | GRVT Maker 成交等待超时（秒） |
| `--post-only-retries` | | `1` | Post-only 订单重试次数 |
| `--signal-cooldown` | | `0` | 信号冷却时间（秒），防止连续追信号 |
| `--log-level` | | `INFO` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `--no-dashboard` | | `false` | 禁用 Rich Dashboard，使用纯文本日志输出 |

### 费率参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--grvt-maker-fee` | `-0.000004` | GRVT maker 费率（负值 = rebate 返佣，-0.0004%） |
| `--grvt-taker-fee` | `0.00042` | GRVT taker 费率（0.042%） |
| `--lighter-taker-fee` | `0` | Lighter taker 费率（0%） |

### Arb 策略参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--long-threshold` | `10` | Long 信号触发阈值（USD 绝对差值） |
| `--short-threshold` | `10` | Short 信号触发阈值（USD 绝对差值） |
| `--min-spread` | `0` | 全局最小价差门槛（USD），低于此值直接忽略 |
| `--natural-spread-window` | `300` | 自然价差滑动窗口样本数（~5min @1样本/s） |
| `--warmup-samples` | `30` | 预热样本数，收集完才开始交易（~30s） |
| `--persistence-count` | `3` | 信号持续确认次数（N × 50ms），过滤闪现假信号 |

### Mean Reversion 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--mr-window` | `300` | 滚动窗口样本数（~5min @1样本/s） |
| `--mr-entry-z` | `2.0` | 入场 z-score 阈值（偏离均值多远入场） |
| `--mr-exit-z` | `0.5` | 出场 z-score 阈值（回归到多近出场） |
| `--mr-stop-z` | `4.0` | 止损 z-score 阈值（价差继续发散时止损） |
| `--mr-maker-timeout` | `60` | GRVT maker 等待超时（秒） |
| `--mr-warmup` | `60` | 预热样本数（均值/标准差收敛后再交易） |

> **约束**：`mr-stop-z > mr-entry-z > mr-exit-z > 0`

### Funding Rate 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--fr-check-interval` | `60` | Rate 检查间隔（秒），funding rate 变化慢 |
| `--fr-min-diff` | `0.0001` | 最小 rate 差异阈值（0.01%），低于不建仓 |
| `--fr-hold-min-hours` | `1` | 最短持仓时间（小时），防止频繁翻转 |

---

## 配置说明 / Configuration

### .env 环境变量

| 变量 | 必需 | 说明 |
|------|:----:|------|
| `GRVT_PRIVATE_KEY` | ✅ | GRVT 签名私钥（0x 开头） |
| `GRVT_API_KEY` | ✅ | GRVT API 密钥 |
| `GRVT_TRADING_ACCOUNT_ID` | ✅ | GRVT 交易子账户 ID |
| `GRVT_ENV` | | `prod` 或 `testnet` |
| `LIGHTER_API_KEY_PRIVATE_KEY` | ✅ | Lighter 签名私钥 |
| `LIGHTER_ACCOUNT_INDEX` | | Lighter 账户索引（默认 `0`） |
| `LIGHTER_API_KEY_INDEX` | | Lighter API Key 索引（默认 `0`） |
| `TG_BOT_TOKEN` | | Telegram Bot Token（不填则禁用） |
| `TG_CHAT_ID` | | Telegram Chat ID |

---

## 风险管理 / Risk Management

### 多层防护体系

```
Layer 1: Pre-trade Check
├── 仓位净暴露 > order_qty × 2  →  跳过交易
├── GRVT 持仓达到 max_position   →  跳过该方向
└── Lighter 持仓达到 max_position →  跳过该方向

Layer 2: Trade Execution Safety
├── GRVT fill 超时 → filled_size = 0, 不开 Lighter 单
├── Lighter size = 确认的 GRVT fill（不是 order_qty）
├── Lighter 对冲失败 → EMERGENCY 告警
└── Lighter fill 超时 → 视为零成交（宁漏不错）

Layer 3: Runtime Monitoring
├── 仓位净暴露 > order_qty × 3 → 紧急停机
├── 余额 < $10 → 双重确认后停机
├── WS 30s 无消息 → 标记 stale, 暂停交易
└── Lighter OB offset gap → 强制重连获取新快照

Layer 4: Graceful Shutdown
├── Ctrl+C 信号 → 屏蔽后续中断
├── 取消所有挂单
├── 3 轮重试平仓（GRVT 2% 封顶，Lighter 递增滑点）
├── 平仓使用 max(API仓位, 本地仓位) — 宁多平不漏平
└── 最终仓位检查 → 未平则 EMERGENCY 告警
```

### 仓位追踪机制

```
本地增量追踪（每笔交易实时更新）
        │
        ▼ 对比
API 周期对账（每 60 秒）
        │
        ▼ 偏差 > 0.0001 → 日志告警 + 使用 API 值覆盖
```

---

## 数据输出 / Data Output

系统运行时会自动在 `data/` 目录生成两类 CSV 文件：

### BBO 快照（每秒记录）

文件名：`bbo_{ticker}_{timestamp}.csv`

```csv
timestamp,grvt_bid,grvt_ask,lighter_bid,lighter_ask,diff_long,diff_short
2026-03-03T08:30:00.123456,95001.2,95002.5,95005.3,95006.1,4.1,-3.6
```

| 列 | 说明 |
|----|------|
| `diff_long` | `lighter_bid - grvt_bid`（正值 = Long GRVT 机会） |
| `diff_short` | `grvt_ask - lighter_ask`（正值 = Short GRVT 机会） |

### 交易记录

文件名：`trades_{ticker}_{timestamp}.csv`

```csv
timestamp,direction,grvt_side,grvt_price,grvt_size,lighter_side,lighter_price,lighter_size,spread,grvt_position,lighter_position
```

### 策略专用数据（MR / FR 模式）

Mean Reversion 模式额外生成 `data/mr_{ticker}_{timestamp}.csv`：
```csv
timestamp,spread,mean,std,z_score,state,direction,grvt_pos,lighter_pos
```

Funding Rate 模式额外生成 `data/fr_{ticker}_{timestamp}.csv`：
```csv
timestamp,grvt_rate,lighter_rate,rate_diff,state,direction,grvt_pos,lighter_pos
```

日志文件输出到 `logs/` 目录。

---

## 推荐参数 / Recommended Parameters

> **重要前提**：没有"保证盈利"的参数组合。以下是基于代码逻辑和市场常识的**保守配置**，核心思路是 **少交易、高胜率、严控风险**。建议先在 testnet 或极小仓位下验证。

### 策略 1：Arb 套利（最成熟，风险最低）

```bash
python arbitrage.py --strategy arb --ticker BTC \
  --size 0.002 --max-position 0.006 \
  --long-threshold 12 --short-threshold 12 \
  --min-spread 3 \
  --signal-cooldown 5 \
  --persistence-count 5 \
  --warmup-samples 60 \
  --natural-spread-window 600
```

| 参数 | 推荐值 | 理由 |
|------|--------|------|
| `threshold=12` | 比默认 10 高 | 只吃大价差，覆盖滑点 + 意外成本 |
| `min-spread=3` | 全局门槛 | 过滤噪音信号 |
| `signal-cooldown=5` | 5 秒冷却 | 防止连续追信号被逆向选择 |
| `persistence-count=5` | 250ms 持续 | 过滤闪现假信号（5 × 50ms） |
| `warmup=60, window=600` | 更长统计窗口 | 自然价差估计更稳定 |
| `size=0.002, max-position=0.006` | 小仓位 | 最多 3 笔叠加，验证后再加量 |

### 策略 2：均值回归（需先观察价差分布）

```bash
python arbitrage.py --strategy mean_reversion --ticker BTC \
  --size 0.002 --max-position 0.004 \
  --mr-window 600 --mr-entry-z 2.5 --mr-exit-z 0.3 --mr-stop-z 3.5 \
  --mr-warmup 120 --mr-maker-timeout 30
```

| 参数 | 推荐值 | 理由 |
|------|--------|------|
| `entry-z=2.5` | 比默认 2.0 严格 | 只在价差显著偏离时入场 |
| `exit-z=0.3` | 接近均值出场 | 吃足回归幅度 |
| `stop-z=3.5` | 比默认 4.0 紧 | 3.5 sigma 已非常极端，及时止损 |
| `window=600` | 10 分钟窗口 | 统计更稳健，减少过拟合 |
| `warmup=120` | 2 分钟预热 | 均值/标准差充分收敛后再交易 |
| `maker-timeout=30` | 30 秒等成交 | 太久价差可能已回归，不值得等 |

> **前提**：先用 arb 模式的 CSV 数据（`data/bbo_*.csv`）分析 GRVT-Lighter 价差是否具有均值回归特性。如果价差是随机游走，MR 策略**必亏**。

### 策略 3：Funding Rate（最慢、最稳，利润最薄）

```bash
python arbitrage.py --strategy funding_rate --ticker BTC \
  --size 0.002 --max-position 0.004 \
  --fr-check-interval 300 --fr-min-diff 0.0003 --fr-hold-min-hours 4
```

| 参数 | 推荐值 | 理由 |
|------|--------|------|
| `check-interval=300` | 5 分钟查一次 | Funding rate 变化慢，不需要频繁查询 |
| `min-diff=0.0003` | 0.03% 差异才建仓 | 默认 0.01% 太敏感，手续费可能吃掉利润 |
| `hold-min-hours=4` | 至少持仓 4 小时 | 给 funding 足够时间结算 |

> **注意**：两个交易所的 funding rate 差异通常很小，扣除手续费后可能不赚钱。适合大资金 + 长期持仓。

### 选择策略的建议

| 顺序 | 策略 | 适合 | 备注 |
|:----:|------|------|------|
| 1 | **Arb** | 首选，最成熟 | 久经考验，bug 修过最多轮 |
| 2 | **Mean Reversion** | 价差有均值回归特性时 | 需要先用 CSV 数据验证，否则必亏 |
| 3 | **Funding Rate** | 大资金长期持仓 | 利润薄，适合 set-and-forget |

> **仓位建议**：`size=0.002` BTC（~$180）足够验证策略。确认盈利模式后再逐步加量，不要一上来就大仓位。

---

## Telegram 通知 / Notifications

配置 `TG_BOT_TOKEN` 和 `TG_CHAT_ID` 后，系统自动发送以下通知：

| 事件 | 频率 | 内容 |
|------|------|------|
| **启动** | 一次 | 配置参数、交易对、阈值 |
| **交易** | 每笔 | 方向、双端价格/数量、价差、利润估算、仓位 |
| **心跳** | 每 5 分钟 | 运行时长、交易次数、当前价差、距触发的差距、仓位 |
| **停止** | 一次 | 停止原因、运行时长、总交易次数 |
| **紧急** | 即时 | 对冲失败、仓位偏差、Lighter fill 超时、平仓失败 |

### 创建 Telegram Bot

1. 在 Telegram 搜索 `@BotFather`
2. 发送 `/newbot`，按提示创建 Bot
3. 记下 Bot Token
4. 给 Bot 发送任意消息
5. 访问 `https://api.telegram.org/bot<TOKEN>/getUpdates` 获取 Chat ID

---

## 项目结构 / Project Structure

```
grvt_lighter/
├── arbitrage.py              # 入口 — 信号处理 + 策略分发 + 优雅关机
├── config.py                 # 配置 — CLI + .env + 三策略参数
├── requirements.txt          # Python 依赖
├── .env.example              # 环境变量模板
├── .gitignore
│
├── exchanges/
│   ├── __init__.py
│   ├── base.py               # 抽象基类 — connect/disconnect/get_position/...
│   ├── grvt_client.py        # GRVT Maker 端 — grvt-pysdk, post-only, WS fill, funding rate
│   └── lighter_client.py     # Lighter Taker 端 — 自定义 WS, FOK, fill event
│
├── strategy/
│   ├── __init__.py
│   ├── arb_strategy.py       # 价差套利 — 50ms 主循环, 健康检查/BBO/价差/风控
│   ├── mean_reversion.py     # 均值回归 — z-score 信号, 状态机, GRVT maker+Lighter taker
│   ├── funding_arb.py        # Funding Rate — rate 差异信号, delta-neutral 持仓
│   ├── order_manager.py      # 交易引擎 — 5 阶段生命周期（arb 专用）
│   ├── spread_analyzer.py    # 价差计算 — bid-bid / ask-ask 比较（arb 专用）
│   ├── position_tracker.py   # 仓位追踪 — 增量 + API 对账（共用）
│   └── order_book_manager.py # BBO 聚合 — 双交易所 facade（共用）
│
├── helpers/
│   ├── __init__.py
│   ├── dashboard.py          # Rich 终端 Dashboard — 6 面板实时可视化
│   ├── logger.py             # 日志（双模式：dashboard/plain）+ CSV DataLogger
│   └── telegram.py           # Telegram 异步通知
│
├── data/                     # (运行时生成) BBO + 交易 + MR/FR CSV
└── logs/                     # (运行时生成) 详细日志文件
```

---

## 核心设计决策 / Design Decisions

### 1. 为什么用 WS 事件确认成交，而不是 REST 轮询？

**教训来源**：前身项目 01-lighter 使用 REST `get_position()` 轮询来确认成交，导致：
- 延迟高（500ms+），期间价格可能已变
- API 502 错误返回空数据，误判为"零仓位"
- 轮询间隔内丢失部分成交信息

**当前方案**：
- GRVT：订阅 `order` stream → `filled_size` 字段实时推送
- Lighter：订阅 `account_orders/{market}/{account}` → `filled_base_amount` 字段
- 使用 `asyncio.Event` 实现零轮询等待

### 2. 为什么 Lighter 使用自定义 WebSocket？

Lighter 官方 `WsClient` 只支持单 channel 订阅。我们需要同时监听：
- `order_book/{market}` — 公开 orderbook 增量更新
- `account_orders/{market}/{account}` — 私有订单更新（需 auth token）

自定义实现支持：
- 双 channel 并行订阅
- Auth token 自动轮转（8 分钟刷新，10 分钟过期）
- Offset 序列验证 + 完整性检查
- 自动重连 + 指数退避

### 3. 为什么 Maker/Taker 分工？

| 考量 | GRVT = Maker | Lighter = Taker |
|------|-------------|-----------------|
| 费率 | Maker rebate（负费率） | 零费率 |
| 确定性 | Post-only 确保挂单 | FOK 确保立即成交或取消 |
| 执行顺序 | 先挂单，等成交 | 确认 Maker fill 后才执行 |
| 风险 | 可能不成交 → 安全 | 可能滑点 → 用 0.2% 缓冲 |

### 4. 为什么 fill timeout = 视为零成交？

01-lighter 项目的致命错误：fill 超时时假设"全额成交"并开出全额对冲单，导致仓位不平衡。

当前安全策略：
```
GRVT fill timeout  → filled_size = 0  → 不开 Lighter 单
Lighter fill timeout → filled_size = 0 → 记录仓位偏差 + EMERGENCY 通知
```

**宁漏不错**：错过一次交易机会的成本远低于一次仓位不平衡。

### 5. 为什么退出时使用 max(API, local) 仓位？

```python
# 选择绝对值更大的仓位来平仓
close_size = max(abs(api_position), abs(local_position))
```

- API 返回 0 可能是因为 502 错误，不是真的没仓位
- 本地追踪可能因为 fill 超时而低估
- **宁多平不漏平**：多平了会被交易所拒绝，漏平了会留下裸头寸

---

## 常见问题 / FAQ

### Q: 支持哪些交易对？

目前支持 `BTC` 和 `ETH`。Ticker 映射：
- `BTC` → GRVT: `BTC_USDT_Perp` / Lighter: `BTC`
- `ETH` → GRVT: `ETH_USDT_Perp` / Lighter: `ETH`

### Q: 阈值怎么设置？

阈值是 USD 绝对差值。建议：
1. 先用 `--log-level DEBUG` 运行，观察 `diff_long` 和 `diff_short` 的分布
2. 查看 `data/bbo_*.csv` 中的历史价差数据
3. 设置在价差分布的 P90 附近（频率和利润的平衡点）

### Q: 为什么使用 `--fill-timeout 5`？

5 秒是 GRVT post-only 订单等待成交的超时。太短会频繁超时浪费 gas，太长会在价格快速变化时被动。建议 3-10 秒范围。

### Q: 可以同时运行多个实例吗？

可以，但需要确保：
- 每个实例使用不同的 `--ticker`
- 或使用不同的交易所子账户
- 日志和数据文件会自动按时间戳分开

### Q: Testnet 怎么测试？

```bash
# .env 中设置
GRVT_ENV=testnet

# 然后正常运行
python arbitrage.py --ticker BTC --size 0.001 --max-position 0.01
```

---

## 免责声明 / Disclaimer

> **本软件仅供学习和研究目的。**
>
> - 加密货币交易具有高风险，可能导致本金全部损失
> - 本软件不构成任何投资建议
> - 作者不对使用本软件产生的任何损失负责
> - 在使用真实资金之前，请务必在 testnet 上充分测试
> - 请遵守您所在司法管辖区的相关法律法规
>
> **USE AT YOUR OWN RISK.** The authors are not responsible for any financial losses incurred from using this software. Always test on testnet before deploying with real funds.

---

<p align="center">
  Made with ⚡ for the DeFi community
</p>
