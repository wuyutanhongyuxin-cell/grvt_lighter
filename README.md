<h1 align="center">
  ⚡ Lighter × GRVT Cross-Exchange Arbitrage Bot
</h1>

<p align="center">
  <b>高频跨交易所永续合约套利系统 — Maker/Taker 模型，WebSocket 事件驱动</b>
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
- [核心特性 / Features](#核心特性--features)
- [终端 Dashboard / Terminal Dashboard](#终端-dashboard--terminal-dashboard)
- [系统架构 / Architecture](#系统架构--architecture)
- [交易流程 / Trade Lifecycle](#交易流程--trade-lifecycle)
- [快速开始 / Quick Start](#快速开始--quick-start)
- [命令行参数 / CLI Reference](#命令行参数--cli-reference)
- [配置说明 / Configuration](#配置说明--configuration)
- [风险管理 / Risk Management](#风险管理--risk-management)
- [数据输出 / Data Output](#数据输出--data-output)
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
- **退出强制平仓** — 3 轮递增滑点（2% → 5% → 10%）保证平仓
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
arbitrage.py                    ← 入口：信号处理 + 优雅关机
    │
    ├── config.py               ← 配置：CLI args + .env
    │
    ├── strategy/
    │   ├── arb_strategy.py     ← 主循环：50ms 健康检查 + 信号检测 + 风控
    │   ├── order_manager.py    ← 交易核心：5 阶段生命周期管理
    │   ├── spread_analyzer.py  ← 价差计算 + 信号触发
    │   ├── position_tracker.py ← 仓位追踪 + API 对账
    │   └── order_book_manager.py ← 双交易所 BBO 聚合
    │
    ├── exchanges/
    │   ├── base.py             ← 交易所抽象基类
    │   ├── grvt_client.py      ← GRVT Maker 端（grvt-pysdk）
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
2. 三轮递增滑点平仓（2% → 5% → 10%）
3. 恢复终端（Dashboard 模式下自动退出全屏）
4. 断开 WebSocket
5. 发送 Telegram 停止通知
6. 关闭数据文件

---

## 命令行参数 / CLI Reference

| 参数 | 必需 | 默认值 | 说明 |
|------|:----:|--------|------|
| `--ticker` | | `BTC` | 交易标的（`BTC`, `ETH`） |
| `--size` | ✅ | — | 每笔交易数量（基础资产单位） |
| `--max-position` | ✅ | — | 单侧最大持仓量 |
| `--long-threshold` | | `10` | Long 信号触发阈值（USD 绝对值） |
| `--short-threshold` | | `10` | Short 信号触发阈值（USD 绝对值） |
| `--fill-timeout` | | `5` | GRVT Maker 成交等待超时（秒） |
| `--log-level` | | `INFO` | 日志级别：`DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `--no-dashboard` | | `false` | 禁用 Rich Dashboard，使用纯文本日志输出 |

### 参数选择建议

**BTC**：
```bash
# 保守配置
--size 0.001 --max-position 0.005 --long-threshold 15 --short-threshold 15

# 激进配置
--size 0.005 --max-position 0.05 --long-threshold 5 --short-threshold 5
```

**ETH**：
```bash
# 保守配置
--size 0.01 --max-position 0.05 --long-threshold 8 --short-threshold 8

# 激进配置
--size 0.05 --max-position 0.5 --long-threshold 3 --short-threshold 3
```

> **阈值说明**：阈值是 USD 绝对差值。BTC 价格约 $95,000 时，$10 阈值 ≈ 1 bps。阈值越低交易越频繁但价差越小。

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
├── 3 轮递增滑点平仓（2% → 5% → 10%）
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

日志文件输出到 `logs/` 目录。

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
├── arbitrage.py              # 入口 — 信号处理 + 优雅关机
├── config.py                 # 配置 — CLI + .env 融合
├── requirements.txt          # Python 依赖
├── .env.example              # 环境变量模板
├── .gitignore
│
├── exchanges/
│   ├── __init__.py
│   ├── base.py               # 抽象基类 — connect/disconnect/get_position/...
│   ├── grvt_client.py        # GRVT Maker 端 — grvt-pysdk, post-only, WS fill
│   └── lighter_client.py     # Lighter Taker 端 — 自定义 WS, FOK, fill event
│
├── strategy/
│   ├── __init__.py
│   ├── arb_strategy.py       # 主循环 — 50ms, 健康检查/BBO/价差/风控/交易
│   ├── order_manager.py      # 交易引擎 — 5 阶段生命周期
│   ├── spread_analyzer.py    # 价差计算 — bid-bid / ask-ask 比较
│   ├── position_tracker.py   # 仓位追踪 — 增量 + API 对账
│   └── order_book_manager.py # BBO 聚合 — 双交易所 facade
│
├── helpers/
│   ├── __init__.py
│   ├── dashboard.py          # Rich 终端 Dashboard — 6 面板实时可视化
│   ├── logger.py             # 日志（双模式：dashboard/plain）+ CSV DataLogger
│   └── telegram.py           # Telegram 异步通知
│
├── data/                     # (运行时生成) BBO + 交易 CSV
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
