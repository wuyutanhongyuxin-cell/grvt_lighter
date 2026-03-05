"""
Rich Terminal Dashboard — real-time visualization for the arbitrage bot.

Replaces wall-of-text logging with a live-updating dashboard showing:
  - Exchange connectivity & health
  - BBO from both sides + spread heatmap
  - Position & risk gauges
  - Recent trades table
  - Rolling event log

Install:  pip install rich

Usage in arb_strategy.py:
    from helpers.dashboard import Dashboard, DashboardLogHandler
    self.dashboard = Dashboard(config)
    self.dashboard.start()
    # in loop:
    self.dashboard.update_bbo(...)
    self.dashboard.refresh()
    # on shutdown:
    self.dashboard.stop()
"""

import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from rich.align import Align
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

logger = logging.getLogger("arbitrage.dashboard")

# ── Palette ──────────────────────────────────────────────────────
C_BG        = "#0d1117"
C_BORDER    = "#30363d"
C_TEXT      = "#c9d1d9"
C_DIM       = "#484f58"
C_ACCENT    = "#58a6ff"
C_GREEN     = "#3fb950"
C_RED       = "#f85149"
C_YELLOW    = "#d29922"
C_CYAN      = "#39d353"
C_ORANGE    = "#db6d28"
C_PURPLE    = "#bc8cff"

NET_POS_WARN = Decimal("0.001")


# ── Helper formatters ────────────────────────────────────────────

def _fmt_price(val: Optional[Decimal], width: int = 10) -> Text:
    if val is None:
        return Text("  —  ".center(width), style=C_DIM)
    return Text(f"{val:>{width},.1f}", style="bold " + C_TEXT)


def _fmt_spread(val: Decimal, threshold: Decimal) -> Text:
    if threshold <= 0:
        ratio = Decimal("0")
    else:
        ratio = val / threshold
    if ratio > Decimal("1"):
        style, icon = f"bold {C_GREEN}", "▲"
    elif ratio > Decimal("0.8"):
        style, icon = f"bold {C_YELLOW}", "△"
    elif ratio > Decimal("0.5"):
        style, icon = C_ORANGE, "○"
    else:
        style, icon = C_DIM, "·"
    return Text(f"{icon} ${val:>8.2f}", style=style)


def _fmt_pos(val: Decimal) -> Text:
    if val > 0:
        return Text(f"+{val:.4f}", style=C_GREEN)
    elif val < 0:
        return Text(f"{val:.4f}", style=C_RED)
    return Text("0.0000", style=C_DIM)


def _bar(ratio: float, width: int = 20, fill_style: str = C_ACCENT) -> Text:
    ratio = max(0.0, min(1.0, ratio))
    filled = int(ratio * width)
    t = Text()
    t.append("━" * filled, style=fill_style)
    t.append("╌" * (width - filled), style=C_DIM)
    return t


# ── Dashboard ────────────────────────────────────────────────────

class Dashboard:
    """Thread-safe live terminal dashboard powered by Rich."""

    def __init__(self, config):
        self.config = config
        self._console = Console()
        self._live: Optional[Live] = None

        # BBO state
        self.grvt_bid: Optional[Decimal] = None
        self.grvt_ask: Optional[Decimal] = None
        self.lighter_bid: Optional[Decimal] = None
        self.lighter_ask: Optional[Decimal] = None
        self.diff_long: Decimal = Decimal("0")
        self.diff_short: Decimal = Decimal("0")

        # Spread analysis state
        self.fee_cost: Decimal = Decimal("0")
        self.natural_spread_long: Decimal = Decimal("0")
        self.natural_spread_short: Decimal = Decimal("0")
        self.net_spread_long: Decimal = Decimal("0")
        self.net_spread_short: Decimal = Decimal("0")
        self.warmed_up: bool = False
        self.warmup_progress: int = 0
        self.warmup_target: int = 0
        self.persist_long: int = 0
        self.persist_short: int = 0
        self.persist_required: int = 1
        self.execution_mode: str = ""

        # Positions
        self.grvt_position: Decimal = Decimal("0")
        self.lighter_position: Decimal = Decimal("0")

        # Counters
        self.total_trades: int = 0
        self.start_time: float = time.time()

        # Health flags
        self.grvt_ws_ok: bool = False
        self.lighter_ws_ok: bool = False
        self.lighter_ob_ok: bool = False
        self.lighter_acct_ok: bool = False
        self.trading_halted: bool = False
        self.halt_reason: str = ""

        # Ring buffers
        self._trades: deque = deque(maxlen=8)
        self._events: deque = deque(maxlen=14)

    # ── Lifecycle ────────────────────────────────────────────────

    def start(self):
        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=4,
            screen=True,
        )
        self._live.start()

    def stop(self):
        if self._live:
            self._live.stop()
            self._live = None

    def refresh(self):
        if self._live:
            self._live.update(self._render())

    # ── Data update methods ──────────────────────────────────────

    def update_bbo(self, grvt_bid, grvt_ask, lighter_bid, lighter_ask):
        self.grvt_bid = grvt_bid
        self.grvt_ask = grvt_ask
        self.lighter_bid = lighter_bid
        self.lighter_ask = lighter_ask

    def update_spread_stats(self, stats: dict):
        self.diff_long = stats["diff_long"]
        self.diff_short = stats["diff_short"]
        self.fee_cost = stats["fee_cost_long"]
        self.natural_spread_long = stats["natural_spread_long"]
        self.natural_spread_short = stats["natural_spread_short"]
        self.net_spread_long = stats["net_spread_long"]
        self.net_spread_short = stats["net_spread_short"]
        self.warmed_up = stats["warmed_up"]
        self.warmup_progress = stats["warmup_progress"]
        self.warmup_target = stats["warmup_target"]
        self.persist_long = stats["persist_long"]
        self.persist_short = stats["persist_short"]
        self.persist_required = stats["persist_required"]
        self.execution_mode = stats.get("execution_mode", "")

    def update_positions(self, grvt_pos, lighter_pos):
        self.grvt_position = grvt_pos
        self.lighter_position = lighter_pos

    def update_health(self, grvt_ws_ok, lighter_ws_ok, lighter_ob_ok,
                      lighter_acct_ok, trading_halted=False, halt_reason=""):
        self.grvt_ws_ok = grvt_ws_ok
        self.lighter_ws_ok = lighter_ws_ok
        self.lighter_ob_ok = lighter_ob_ok
        self.lighter_acct_ok = lighter_acct_ok
        self.trading_halted = trading_halted
        self.halt_reason = halt_reason

    def add_trade(self, direction, grvt_price, lighter_price, size, spread,
                  profit_est):
        self._trades.appendleft({
            "time": datetime.now(timezone.utc),
            "dir": direction,
            "gp": grvt_price,
            "lp": lighter_price,
            "sz": size,
            "sp": spread,
            "pnl": profit_est,
        })

    def add_event(self, level: str, msg: str):
        self._events.appendleft({
            "time": datetime.now(timezone.utc),
            "level": level,
            "msg": msg,
        })

    # ── Layout ───────────────────────────────────────────────────

    def _render(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=3),
        )
        layout["body"].split_column(
            Layout(name="top", size=14),
            Layout(name="mid", size=12),
            Layout(name="bot"),
        )
        layout["top"].split_row(
            Layout(name="health", ratio=1),
            Layout(name="bbo", ratio=2),
            Layout(name="spread", ratio=1),
        )
        layout["mid"].split_row(
            Layout(name="positions", ratio=1),
            Layout(name="trades", ratio=2),
        )

        layout["header"].update(self._hdr())
        layout["health"].update(self._health())
        layout["bbo"].update(self._bbo())
        layout["spread"].update(self._spread())
        layout["positions"].update(self._pos())
        layout["trades"].update(self._trd())
        layout["bot"].update(self._evts())
        layout["footer"].update(self._ftr())
        return layout

    # ── Panels ───────────────────────────────────────────────────

    def _hdr(self) -> Panel:
        uptime = timedelta(seconds=int(time.time() - self.start_time))
        t = Text()
        t.append("  ⚡ ", style=C_YELLOW)
        t.append("LIGHTER", style=f"bold {C_CYAN}")
        t.append(" × ", style=C_DIM)
        t.append("GRVT", style=f"bold {C_PURPLE}")
        t.append(f"  ─  {self.config.ticker}-PERP", style=f"bold {C_TEXT}")
        t.append(f"    ⏱ {uptime}", style=C_DIM)
        t.append(f"    📊 {self.total_trades} trades", style=C_ACCENT)
        if self.trading_halted:
            t.append(f"    ⛔ HALTED: {self.halt_reason}", style=f"bold {C_RED}")
        return Panel(Align.center(t), style=f"on {C_BG}", border_style=C_BORDER)

    def _health(self) -> Panel:
        def _s(ok, label):
            t = Text()
            t.append(" ● " if ok else " ○ ", style=C_GREEN if ok else C_RED)
            t.append(label + "\n", style=C_TEXT if ok else f"dim {C_RED}")
            return t

        t = Text()
        t.append("GRVT\n", style=f"bold {C_PURPLE}")
        t.append_text(_s(self.grvt_ws_ok, "WebSocket"))
        t.append("\n")
        t.append("LIGHTER\n", style=f"bold {C_CYAN}")
        t.append_text(_s(self.lighter_ws_ok, "WebSocket"))
        t.append_text(_s(self.lighter_ob_ok, "Orderbook"))
        t.append_text(_s(self.lighter_acct_ok, "AcctOrders"))
        return Panel(t, title="[bold]Health[/]", title_align="left",
                     border_style=C_BORDER, style=f"on {C_BG}", padding=(0, 1))

    def _bbo(self) -> Panel:
        tbl = Table(show_header=True, header_style=f"bold {C_DIM}",
                    box=None, padding=(0, 2), expand=True)
        tbl.add_column("", width=10)
        tbl.add_column("Bid", justify="right", width=14)
        tbl.add_column("Ask", justify="right", width=14)
        tbl.add_column("Spread", justify="right", width=10)

        def _row_spread(bid, ask):
            if bid and ask:
                return Text(f"${ask - bid:.1f}", style=C_DIM)
            return Text("—", style=C_DIM)

        tbl.add_row(Text("GRVT", style=f"bold {C_PURPLE}"),
                     _fmt_price(self.grvt_bid), _fmt_price(self.grvt_ask),
                     _row_spread(self.grvt_bid, self.grvt_ask))
        tbl.add_row(Text("Lighter", style=f"bold {C_CYAN}"),
                     _fmt_price(self.lighter_bid), _fmt_price(self.lighter_ask),
                     _row_spread(self.lighter_bid, self.lighter_ask))

        # Cross-exchange delta
        tbl.add_row(Text(""), Text(""), Text(""), Text(""))
        cb = Text("—", style=C_DIM)
        ca = Text("—", style=C_DIM)
        if self.lighter_bid and self.grvt_bid:
            d = self.lighter_bid - self.grvt_bid
            cb = Text(f"Δ {d:+.1f}", style=C_GREEN if d > 0 else C_RED)
        if self.grvt_ask and self.lighter_ask:
            d = self.grvt_ask - self.lighter_ask
            ca = Text(f"Δ {d:+.1f}", style=C_GREEN if d > 0 else C_RED)
        tbl.add_row(Text("Cross Δ", style=f"bold {C_YELLOW}"), cb, ca, Text(""))

        return Panel(tbl, title="[bold]BBO — Best Bid / Ask[/]", title_align="left",
                     border_style=C_BORDER, style=f"on {C_BG}")

    def _spread(self) -> Panel:
        lt = self.config.long_threshold
        st = self.config.short_threshold
        t = Text()

        # Warmup indicator
        if not self.warmed_up and self.warmup_target > 0:
            wp = self.warmup_progress
            wt = self.warmup_target
            t.append(f"WARMUP {wp}/{wt}\n", style=f"bold {C_YELLOW}")
            t.append_text(_bar(float(wp / wt) if wt > 0 else 0, 18, C_YELLOW))
            t.append("\n\n")

        # Long
        t.append("LONG GRVT\n", style=f"bold {C_GREEN}")
        t.append(f" raw  $ {self.diff_long:>8.2f}\n", style=C_TEXT)
        t.append(f" fee  $ {self.fee_cost:>8.2f}\n", style=C_DIM)
        t.append(f" nat  $ {self.natural_spread_long:>8.2f}\n", style=C_DIM)
        net_l_style = C_GREEN if self.net_spread_long > lt else C_TEXT
        persist_l = f"[{self.persist_long}]" if self.persist_required > 1 else ""
        t.append(f" net  ", style=C_DIM)
        t.append_text(_fmt_spread(self.net_spread_long, lt))
        t.append(f" / ${lt:.0f}  {persist_l}\n", style=C_DIM)
        rl = float(self.net_spread_long / lt) if lt > 0 else 0
        t.append(" ")
        t.append_text(_bar(rl, 18,
                           C_GREEN if rl > 1 else C_YELLOW if rl > .8 else C_ACCENT))
        t.append("\n\n")

        # Short
        t.append("SHORT GRVT\n", style=f"bold {C_RED}")
        t.append(f" raw  $ {self.diff_short:>8.2f}\n", style=C_TEXT)
        t.append(f" fee  $ {self.fee_cost:>8.2f}\n", style=C_DIM)
        t.append(f" nat  $ {self.natural_spread_short:>8.2f}\n", style=C_DIM)
        persist_s = f"[{self.persist_short}]" if self.persist_required > 1 else ""
        t.append(f" net  ", style=C_DIM)
        t.append_text(_fmt_spread(self.net_spread_short, st))
        t.append(f" / ${st:.0f}  {persist_s}\n", style=C_DIM)
        rs = float(self.net_spread_short / st) if st > 0 else 0
        t.append(" ")
        t.append_text(_bar(rs, 18,
                           C_GREEN if rs > 1 else C_YELLOW if rs > .8 else C_ACCENT))

        return Panel(t, title="[bold]Spread → Signal[/]", title_align="left",
                     border_style=C_BORDER, style=f"on {C_BG}", padding=(0, 1))

    def _pos(self) -> Panel:
        net = self.grvt_position + self.lighter_position
        mx = self.config.max_position
        t = Text()

        t.append("GRVT       ", style=f"bold {C_PURPLE}")
        t.append_text(_fmt_pos(self.grvt_position))
        if mx > 0:
            r = float(abs(self.grvt_position) / mx)
            t.append("  ")
            t.append_text(_bar(r, 12, C_PURPLE if r < .8 else C_RED))
        t.append("\n")

        t.append("Lighter    ", style=f"bold {C_CYAN}")
        t.append_text(_fmt_pos(self.lighter_position))
        if mx > 0:
            r = float(abs(self.lighter_position) / mx)
            t.append("  ")
            t.append_text(_bar(r, 12, C_CYAN if r < .8 else C_RED))
        t.append("\n")

        t.append("─" * 32 + "\n", style=C_DIM)
        nc = C_GREEN if abs(net) < NET_POS_WARN else C_RED
        t.append("Net exp.   ", style=f"bold {C_YELLOW}")
        t.append(f"{net:+.6f}\n", style=f"bold {nc}")
        t.append(f"Max ±{mx}  Qty {self.config.order_quantity}\n", style=C_DIM)

        return Panel(t, title="[bold]Positions[/]", title_align="left",
                     border_style=C_BORDER, style=f"on {C_BG}", padding=(0, 1))

    def _trd(self) -> Panel:
        tbl = Table(show_header=True, header_style=f"bold {C_DIM}",
                    box=None, padding=(0, 1), expand=True)
        tbl.add_column("Time", width=8)
        tbl.add_column("Dir", width=6)
        tbl.add_column("GRVT", justify="right", width=11)
        tbl.add_column("Lighter", justify="right", width=11)
        tbl.add_column("Size", justify="right", width=8)
        tbl.add_column("Spread", justify="right", width=9)
        tbl.add_column("P&L", justify="right", width=10)

        for tr in self._trades:
            sc = C_GREEN if tr["sp"] > 0 else C_RED
            pc = C_GREEN if tr["pnl"] > 0 else C_RED
            tbl.add_row(
                Text(tr["time"].strftime("%H:%M:%S"), style=C_DIM),
                Text("LONG" if "long" in tr["dir"] else "SHORT",
                     style=C_GREEN if "long" in tr["dir"] else C_RED),
                Text(f"${tr['gp']:,.1f}", style=C_PURPLE),
                Text(f"${tr['lp']:,.1f}", style=C_CYAN),
                Text(f"{tr['sz']}", style=C_TEXT),
                Text(f"${tr['sp']:+.2f}", style=sc),
                Text(f"${tr['pnl']:+.4f}", style=f"bold {pc}"),
            )

        if not self._trades:
            tbl.add_row(Text(""), Text(""), Text(""),
                        Text("waiting for signals…", style=C_DIM),
                        Text(""), Text(""), Text(""))

        return Panel(tbl, title="[bold]Recent Trades[/]", title_align="left",
                     border_style=C_BORDER, style=f"on {C_BG}")

    def _evts(self) -> Panel:
        t = Text()
        for ev in self._events:
            ts = ev["time"].strftime("%H:%M:%S")
            lv = ev["level"].upper()
            if lv in ("ERROR", "EMERGENCY"):
                s, ic = C_RED, "✖"
            elif lv == "WARNING":
                s, ic = C_YELLOW, "⚠"
            elif lv == "TRADE":
                s, ic = C_GREEN, "✦"
            else:
                s, ic = C_DIM, "›"
            t.append(f" {ts} ", style=C_DIM)
            t.append(f"{ic} ", style=s)
            t.append(f"{ev['msg'][:100]}\n", style=s)

        if not self._events:
            t.append("  waiting for events…\n", style=C_DIM)

        return Panel(t, title="[bold]Event Log[/]", title_align="left",
                     border_style=C_BORDER, style=f"on {C_BG}")

    def _ftr(self) -> Panel:
        t = Text()
        t.append(" Ctrl+C", style=f"bold {C_YELLOW}")
        t.append(" exit  ", style=C_DIM)
        t.append("│", style=C_BORDER)
        mode_label = self.execution_mode or self.config.execution_mode
        t.append(f" {mode_label}", style=C_ACCENT)
        t.append(" │", style=C_BORDER)
        t.append(f" fee=${self.fee_cost:.2f}", style=C_DIM)
        t.append(" │", style=C_BORDER)
        t.append(f" L=${self.config.long_threshold}", style=C_GREEN)
        t.append(f" S=${self.config.short_threshold}", style=C_RED)
        t.append(" │", style=C_BORDER)
        t.append(f" timeout={self.config.fill_timeout}s", style=C_DIM)
        t.append(" │", style=C_BORDER)
        t.append(f" cooldown={self.config.signal_cooldown}s", style=C_DIM)
        return Panel(t, style=f"on {C_BG}", border_style=C_BORDER)


# ── Log Handler → Dashboard events ──────────────────────────────

class DashboardLogHandler(logging.Handler):
    """Forward WARNING+ logs from strategy modules to the dashboard event panel."""

    FORWARD_LOGGERS = {
        "arbitrage.orders", "arbitrage.strategy", "arbitrage.grvt",
        "arbitrage.lighter", "arbitrage.position", "arbitrage.main",
    }

    def __init__(self, dashboard: Dashboard):
        super().__init__(level=logging.WARNING)
        self.dashboard = dashboard

    def emit(self, record: logging.LogRecord):
        try:
            if record.name in self.FORWARD_LOGGERS:
                msg = self.format(record) if self.formatter else record.getMessage()
                self.dashboard.add_event(record.levelname, msg)
        except Exception:
            pass
