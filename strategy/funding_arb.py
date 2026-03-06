"""
Funding Rate Arbitrage Strategy — delta-neutral funding rate capture.

Signal: funding rate differential between GRVT and Lighter.
Execution: GRVT maker (post-only) + Lighter taker (IOC).
Interface matches ArbStrategy: initialize(), run(), shutdown(), request_stop().
"""

import asyncio
import csv
import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import lighter

from config import Config
from exchanges.grvt_client import GrvtClient
from exchanges.lighter_client import LighterClient
from helpers.logger import DataLogger
from helpers.telegram import TelegramNotifier
from strategy.order_book_manager import OrderBookManager
from strategy.position_tracker import PositionTracker

logger = logging.getLogger("arbitrage.fr")

# Constants
ZERO = Decimal("0")
BALANCE_CHECK_INTERVAL = 10  # seconds
HEARTBEAT_INTERVAL = 300  # seconds (5 minutes)
MIN_BALANCE = Decimal("10")
SHUTDOWN_SLIPPAGE_LEVELS = [Decimal("0.05"), Decimal("0.10"), Decimal("0.20")]
REST_POLL_INTERVAL = 0.5  # seconds
CANCEL_RPC_TIMEOUT = 2.0  # seconds
LIGHTER_FILL_TIMEOUT = 2  # seconds
GRVT_MAKER_TIMEOUT = 120  # seconds (longer wait for FR, not urgent)
RECONCILE_INTERVAL = 60  # seconds


class FundingArbStrategy:
    """
    Funding Rate arbitrage: delta-neutral position to capture funding differential.

    Execution: GRVT maker (post-only) + Lighter taker (IOC).
    Interface matches ArbStrategy: initialize(), run(), shutdown(), request_stop().
    """

    def __init__(self, config: Config):
        self.config = config
        self._stop_flag = False
        self._stop_reason = "unknown"
        self._start_time = time.time()

        # Clients
        self.grvt_client = GrvtClient(
            private_key=config.grvt_private_key,
            api_key=config.grvt_api_key,
            trading_account_id=config.grvt_trading_account_id,
            env=config.grvt_env,
            ticker=config.ticker,
        )
        self.lighter_client = LighterClient(
            private_key=config.lighter_private_key,
            account_index=config.lighter_account_index,
            api_key_index=config.lighter_api_key_index,
            ticker=config.ticker,
        )

        # Strategy components
        self.ob_manager = OrderBookManager(self.grvt_client, self.lighter_client)
        self.positions = PositionTracker(config.order_quantity, config.max_position)
        self.data_logger = DataLogger(config.ticker)
        self.telegram = TelegramNotifier(config.tg_bot_token, config.tg_chat_id)

        # FR-specific state
        self._state: str = "flat"  # flat / positioned
        self._position_direction: Optional[str] = None  # "short_grvt" / "long_grvt"
        self._current_grvt_rate: Decimal = ZERO
        self._current_lighter_rate: Decimal = ZERO
        self._rate_diff: Decimal = ZERO
        self._entry_time: float = 0
        self._last_rate_check: float = 0
        self._fr_trades: int = 0
        self._executing: bool = False
        self._trading_halted: bool = False
        self._halt_reason: str = ""

        # Timers
        self._last_balance_check: float = 0
        self._last_heartbeat: float = 0
        self._low_balance_first_check: Optional[float] = None
        self._last_reconcile: float = 0

        # CSV logger
        data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(data_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._fr_csv_path = os.path.join(data_dir, f"fr_{config.ticker}_{ts}.csv")
        self._fr_csv_file = open(self._fr_csv_path, "w", newline="", encoding="utf-8")
        self._fr_csv_writer = csv.writer(self._fr_csv_file)
        self._fr_csv_writer.writerow([
            "timestamp", "grvt_rate", "lighter_rate", "rate_diff",
            "state", "direction", "grvt_pos", "lighter_pos",
        ])

    # ========== Interface ==========

    async def initialize(self) -> None:
        logger.info("Initializing FR strategy...")
        logger.info(
            f"FR Config: ticker={self.config.ticker} size={self.config.order_quantity} "
            f"max_pos={self.config.max_position} "
            f"check_interval={self.config.fr_check_interval}s "
            f"min_diff={self.config.fr_min_diff} "
            f"hold_min_hours={self.config.fr_hold_min_hours}"
        )

        await self.grvt_client.connect()
        await self.lighter_client.connect()

        # Initial position load
        try:
            self.positions.grvt_position = await self.grvt_client.get_position()
            self.positions.lighter_position = await self.lighter_client.get_position()
            logger.info(
                f"Initial positions: GRVT={self.positions.grvt_position} "
                f"Lighter={self.positions.lighter_position}"
            )
        except Exception as e:
            logger.error(f"FATAL: Failed to load initial positions: {e}")
            raise RuntimeError(f"Cannot start without position data: {e}") from e

        # Resume if positioned
        if self.positions.grvt_position != ZERO or self.positions.lighter_position != ZERO:
            self._state = "positioned"
            self._entry_time = time.time()
            if self.positions.grvt_position < ZERO:
                self._position_direction = "short_grvt"
            elif self.positions.grvt_position > ZERO:
                self._position_direction = "long_grvt"
            logger.info(f"Resuming in POSITIONED state: direction={self._position_direction}")

        await self.telegram.send_message(
            f"*FR Strategy Started*\n"
            f"Pair: {self.config.ticker} | Size: {self.config.order_quantity}\n"
            f"Check interval: {self.config.fr_check_interval}s | "
            f"Min diff: {self.config.fr_min_diff} | "
            f"Hold min: {self.config.fr_hold_min_hours}h"
        )

        self._start_time = time.time()
        logger.info("FR strategy initialized")

    async def run(self) -> None:
        logger.info("FR: Starting main loop (1s/iteration)")
        loop_count = 0

        while not self._stop_flag:
            try:
                await self._main_loop_iteration(loop_count)
            except Exception as e:
                logger.error(f"FR loop iteration error: {e}", exc_info=True)

            loop_count += 1
            await asyncio.sleep(1.0)  # 1s intervals — funding rate doesn't need 50ms

        logger.info(f"FR main loop exited: {self._stop_reason}")

    def request_stop(self, reason: str = "user interrupt"):
        self._stop_flag = True
        self._stop_reason = reason
        logger.info(f"FR stop requested: {reason}")

    # ========== Main Loop ==========

    async def _main_loop_iteration(self, loop_count: int):
        now = time.time()

        # WS health check
        if self.grvt_client.is_ws_stale():
            if loop_count % 30 == 0:
                logger.warning("FR: GRVT WS stale")
            return
        if self.lighter_client.is_ws_stale():
            if loop_count % 30 == 0:
                logger.warning("FR: Lighter WS stale")
            return

        # Trading halt
        if self._trading_halted:
            if loop_count % 30 == 0:
                logger.warning(f"FR trading halted: {self._halt_reason}")
            self.request_stop(f"FR_{self._halt_reason}")
            return

        # Balance check
        if now - self._last_balance_check >= BALANCE_CHECK_INTERVAL:
            self._last_balance_check = now
            await self._check_balance()

        # Risk check
        if not self.positions.check_risk():
            self.request_stop("POSITION DIVERGENCE")
            await self.telegram.notify_emergency(
                f"FR Position divergence! "
                f"GRVT={self.positions.grvt_position} "
                f"Lighter={self.positions.lighter_position}"
            )
            return

        # Position reconciliation
        if now - self._last_reconcile >= RECONCILE_INTERVAL:
            self._last_reconcile = now
            await self.positions.reconcile_with_api(self.grvt_client, self.lighter_client)

        # Heartbeat
        if now - self._last_heartbeat >= HEARTBEAT_INTERVAL:
            self._last_heartbeat = now
            await self.telegram.send_message(
                f"*FR Heartbeat* | Runtime {(now - self._start_time) / 3600:.1f}h | "
                f"Trades: {self._fr_trades}\n"
                f"GRVT rate: {self._current_grvt_rate:.6f} | "
                f"Lighter rate: {self._current_lighter_rate:.6f} | "
                f"diff: {self._rate_diff:.6f}\n"
                f"State: {self._state} dir={self._position_direction}\n"
                f"GRVT: {self.positions.grvt_position} | Lighter: {self.positions.lighter_position}"
            )

        # Rate check interval
        if now - self._last_rate_check < self.config.fr_check_interval:
            return
        self._last_rate_check = now

        # Fetch funding rates
        try:
            grvt_rate = await self.grvt_client.get_funding_rate()
        except Exception as e:
            logger.warning(f"FR GRVT rate fetch failed: {e}")
            return
        try:
            lighter_rate = await self._get_lighter_funding_rate()
        except Exception as e:
            logger.warning(f"FR Lighter rate fetch failed: {e}")
            return

        rate_diff = grvt_rate - lighter_rate
        self._current_grvt_rate = grvt_rate
        self._current_lighter_rate = lighter_rate
        self._rate_diff = rate_diff

        logger.info(
            f"FR rates: GRVT={grvt_rate:.6f} Lighter={lighter_rate:.6f} "
            f"diff={rate_diff:.6f} state={self._state}"
        )

        # Log to CSV
        self._log_fr_data()

        if self._executing:
            return

        if self._state == "flat":
            if rate_diff > self.config.fr_min_diff:
                # GRVT rate high → short GRVT to receive funding
                logger.info(f"FR SIGNAL: SHORT GRVT (diff={rate_diff:.6f} > {self.config.fr_min_diff})")
                await self._build_position("short_grvt")
            elif rate_diff < -self.config.fr_min_diff:
                # Lighter rate high → long GRVT
                logger.info(f"FR SIGNAL: LONG GRVT (diff={rate_diff:.6f} < -{self.config.fr_min_diff})")
                await self._build_position("long_grvt")

        elif self._state == "positioned":
            holding_hours = (now - self._entry_time) / 3600
            if self._should_exit(rate_diff, holding_hours):
                logger.info(
                    f"FR EXIT: diff={rate_diff:.6f} holding={holding_hours:.1f}h "
                    f"dir={self._position_direction}"
                )
                await self._close_fr_position()

    # ========== Funding Rate Fetch ==========

    async def _get_lighter_funding_rate(self) -> Decimal:
        """Get Lighter funding rate via FundingApi."""
        funding_api = lighter.FundingApi(self.lighter_client._api_client)
        result = await funding_api.funding_rates()
        if result and hasattr(result, "funding_rates"):
            for rate_info in result.funding_rates:
                if hasattr(rate_info, "market_id") and rate_info.market_id == self.lighter_client.market_index:
                    rate = getattr(
                        rate_info, "current_funding_rate",
                        getattr(rate_info, "funding_rate", 0),
                    )
                    return Decimal(str(rate)) if rate else ZERO
        return ZERO

    # ========== Exit Logic ==========

    def _should_exit(self, rate_diff: Decimal, holding_hours: float) -> bool:
        if holding_hours < float(self.config.fr_hold_min_hours):
            return False
        # Rate flipped
        if self._position_direction == "short_grvt" and rate_diff < ZERO:
            return True
        if self._position_direction == "long_grvt" and rate_diff > ZERO:
            return True
        # Rate diff below threshold
        if abs(rate_diff) < self.config.fr_min_diff:
            return True
        return False

    # ========== Execution ==========

    async def _build_position(self, direction: str):
        """Build delta-neutral position: GRVT maker + Lighter taker."""
        if self._executing:
            return
        self._executing = True

        try:
            if direction == "short_grvt":
                grvt_side, lighter_side = "sell", "buy"
            else:
                grvt_side, lighter_side = "buy", "sell"

            # Capacity check
            if grvt_side == "buy" and not self.positions.can_long_grvt():
                logger.info("FR: Max long position reached, skipping")
                return
            if grvt_side == "sell" and not self.positions.can_short_grvt():
                logger.info("FR: Max short position reached, skipping")
                return

            # Phase 0: Refresh positions
            await self._refresh_positions()
            pre_pos_grvt = self.positions.grvt_position

            if not self.positions.check_pre_trade():
                logger.warning("FR: Pre-trade position check failed, skipping")
                return

            # Phase 1: GRVT post-only order
            grvt_bid, grvt_ask = self.grvt_client.get_bbo()
            if grvt_bid is None or grvt_ask is None:
                logger.warning("FR: GRVT BBO not available")
                return

            tick = self.grvt_client.tick_size
            if grvt_side == "buy":
                price = grvt_ask - tick
            else:
                price = grvt_bid + tick

            try:
                grvt_cid = await self.grvt_client.place_post_only_order(
                    grvt_side, price, self.config.order_quantity,
                )
            except Exception as e:
                logger.error(f"FR GRVT place order failed: {e}")
                return

            # Phase 2: Wait for GRVT fill (longer timeout for FR)
            logger.info(f"FR Phase 2: Waiting for GRVT fill (timeout={GRVT_MAKER_TIMEOUT}s)")
            filled_qty = await self._wait_grvt_fill(
                grvt_cid, pre_pos_grvt, grvt_side, price,
                timeout=GRVT_MAKER_TIMEOUT,
            )

            if filled_qty <= ZERO:
                logger.info("FR: GRVT maker not filled, staying FLAT")
                try:
                    await asyncio.wait_for(
                        self.grvt_client.cancel_all_orders(), timeout=CANCEL_RPC_TIMEOUT,
                    )
                except (asyncio.TimeoutError, Exception) as e:
                    logger.warning(f"FR cancel_all after no fill: {e}")
                return

            # Phase 3: Lighter IOC hedge
            pre_pos_lighter = self.positions.lighter_position
            logger.info(f"FR Phase 3: Lighter IOC {lighter_side} {filled_qty}")
            try:
                lighter_idx = await self.lighter_client.place_ioc_order(
                    side=lighter_side, size=filled_qty,
                )
            except Exception as e:
                logger.error(
                    f"FR LIGHTER HEDGE FAILED! GRVT {grvt_side} {filled_qty} filled, "
                    f"Lighter {lighter_side} error: {e}"
                )
                self.positions.update_grvt(grvt_side, filled_qty)
                self._trading_halted = True
                self._halt_reason = "HEDGE_SUBMIT_FAILED"
                await self.telegram.notify_emergency(
                    f"FR HEDGE FAILURE: GRVT {grvt_side} {filled_qty} filled, "
                    f"Lighter {lighter_side} failed: {e}"
                )
                return

            # Phase 4: Wait for Lighter fill
            lighter_filled = await self._wait_lighter_fill(
                lighter_idx, pre_pos_lighter, lighter_side, filled_qty,
            )

            if lighter_filled <= ZERO:
                self.positions.update_grvt(grvt_side, filled_qty)
                logger.error(f"FR LIGHTER FILL UNKNOWN! GRVT {grvt_side} {filled_qty}")
                self._trading_halted = True
                self._halt_reason = "HEDGE_FILL_UNKNOWN"
                await self.telegram.notify_emergency(
                    f"FR LIGHTER FILL TIMEOUT: GRVT {grvt_side} {filled_qty}, "
                    f"Lighter {lighter_side} fill unknown"
                )
                return

            # Phase 5: Update state
            self.positions.update_grvt(grvt_side, filled_qty)
            self.positions.update_lighter(lighter_side, lighter_filled)
            self._state = "positioned"
            self._position_direction = direction
            self._entry_time = time.time()
            self._fr_trades += 1

            logger.info(
                f"FR POSITIONED: {direction} "
                f"GRVT {grvt_side} {filled_qty} / Lighter {lighter_side} {lighter_filled} "
                f"rate_diff={self._rate_diff:.6f} "
                f"pos: G={self.positions.grvt_position} L={self.positions.lighter_position}"
            )

            self.data_logger.log_trade(
                direction=f"fr_{direction}",
                grvt_side=grvt_side, grvt_price=price, grvt_size=filled_qty,
                lighter_side=lighter_side, lighter_price=ZERO, lighter_size=lighter_filled,
                spread=self._rate_diff,
                grvt_position=self.positions.grvt_position,
                lighter_position=self.positions.lighter_position,
            )

            await self.telegram.send_message(
                f"*FR Entry: {direction}*\n"
                f"Rate diff: {self._rate_diff:.6f}\n"
                f"GRVT {grvt_side} {filled_qty} | Lighter {lighter_side} {lighter_filled}\n"
                f"Pos: G={self.positions.grvt_position} L={self.positions.lighter_position}"
            )

        except Exception as e:
            logger.error(f"FR build_position unexpected error: {e}", exc_info=True)
        finally:
            self._executing = False

    async def _close_fr_position(self):
        """Close delta-neutral position: GRVT maker + Lighter taker."""
        if self._executing:
            return
        self._executing = True

        try:
            if self._position_direction == "short_grvt":
                # Was sell GRVT → exit: buy GRVT, sell Lighter
                grvt_side, lighter_side = "buy", "sell"
            else:
                grvt_side, lighter_side = "sell", "buy"

            # Phase 0: Refresh positions
            await self._refresh_positions()
            pre_pos_grvt = self.positions.grvt_position

            exit_size = abs(self.positions.grvt_position)
            if exit_size <= ZERO:
                logger.info("FR: No position to close, back to FLAT")
                self._state = "flat"
                self._position_direction = None
                return

            exit_size = min(exit_size, self.config.order_quantity)

            # Phase 1: GRVT post-only
            grvt_bid, grvt_ask = self.grvt_client.get_bbo()
            if grvt_bid is None or grvt_ask is None:
                logger.warning("FR exit: GRVT BBO not available")
                return

            tick = self.grvt_client.tick_size
            if grvt_side == "buy":
                price = grvt_ask - tick
            else:
                price = grvt_bid + tick

            try:
                grvt_cid = await self.grvt_client.place_post_only_order(
                    grvt_side, price, exit_size,
                )
            except Exception as e:
                logger.error(f"FR exit: GRVT place order failed: {e}")
                return

            # Phase 2: Wait for GRVT fill
            filled_qty = await self._wait_grvt_fill(
                grvt_cid, pre_pos_grvt, grvt_side, price,
                timeout=GRVT_MAKER_TIMEOUT,
            )

            if filled_qty <= ZERO:
                logger.info("FR exit: GRVT maker not filled, staying POSITIONED")
                try:
                    await asyncio.wait_for(
                        self.grvt_client.cancel_all_orders(), timeout=CANCEL_RPC_TIMEOUT,
                    )
                except (asyncio.TimeoutError, Exception) as e:
                    logger.warning(f"FR exit cancel_all: {e}")
                return

            # Phase 3: Lighter IOC
            pre_pos_lighter = self.positions.lighter_position
            try:
                lighter_idx = await self.lighter_client.place_ioc_order(
                    side=lighter_side, size=filled_qty,
                )
            except Exception as e:
                logger.error(
                    f"FR EXIT LIGHTER HEDGE FAILED! GRVT {grvt_side} {filled_qty}, "
                    f"Lighter error: {e}"
                )
                self.positions.update_grvt(grvt_side, filled_qty)
                self._trading_halted = True
                self._halt_reason = "EXIT_HEDGE_SUBMIT_FAILED"
                await self.telegram.notify_emergency(
                    f"FR EXIT HEDGE FAILURE: GRVT {grvt_side} {filled_qty}, "
                    f"Lighter {lighter_side} failed: {e}"
                )
                return

            # Phase 4: Wait for Lighter fill
            lighter_filled = await self._wait_lighter_fill(
                lighter_idx, pre_pos_lighter, lighter_side, filled_qty,
            )

            if lighter_filled <= ZERO:
                self.positions.update_grvt(grvt_side, filled_qty)
                self._trading_halted = True
                self._halt_reason = "EXIT_HEDGE_FILL_UNKNOWN"
                await self.telegram.notify_emergency(
                    f"FR EXIT LIGHTER TIMEOUT: GRVT {grvt_side} {filled_qty}, "
                    f"Lighter {lighter_side} fill unknown"
                )
                return

            # Phase 5: Update
            self.positions.update_grvt(grvt_side, filled_qty)
            self.positions.update_lighter(lighter_side, lighter_filled)
            self._fr_trades += 1

            if abs(self.positions.grvt_position) < self.config.order_quantity * Decimal("0.01"):
                self._state = "flat"
                self._position_direction = None
                logger.info("FR FLAT: position fully closed")
            else:
                logger.info(
                    f"FR: Partial exit, remaining G={self.positions.grvt_position}, staying POSITIONED"
                )

            self.data_logger.log_trade(
                direction=f"fr_exit_{self._position_direction or 'unknown'}",
                grvt_side=grvt_side, grvt_price=price, grvt_size=filled_qty,
                lighter_side=lighter_side, lighter_price=ZERO, lighter_size=lighter_filled,
                spread=self._rate_diff,
                grvt_position=self.positions.grvt_position,
                lighter_position=self.positions.lighter_position,
            )

            await self.telegram.send_message(
                f"*FR Exit*\n"
                f"Rate diff: {self._rate_diff:.6f}\n"
                f"GRVT {grvt_side} {filled_qty} | Lighter {lighter_side} {lighter_filled}\n"
                f"Pos: G={self.positions.grvt_position} L={self.positions.lighter_position}"
            )

        except Exception as e:
            logger.error(f"FR close_position unexpected error: {e}", exc_info=True)
        finally:
            self._executing = False

    # ========== Fill Detection ==========

    async def _wait_grvt_fill(
        self,
        client_order_id: str,
        pre_pos: Decimal,
        side: str,
        price: Decimal,
        timeout: float,
    ) -> Decimal:
        """Wait for GRVT fill via WS + REST concurrent polling."""
        deadline = time.time() + timeout

        while time.time() < deadline:
            remaining = max(deadline - time.time(), 0.01)
            ws_wait = min(REST_POLL_INTERVAL, remaining)

            fill_data = await self.grvt_client.wait_for_fill(
                client_order_id, timeout=ws_wait, keep_pending_on_timeout=True,
            )
            if fill_data is not None:
                filled = fill_data.get("filled_size", ZERO)
                if filled > ZERO:
                    filled = min(filled, self.config.order_quantity)
                    logger.info(f"FR GRVT fill via WS: {filled}")
                    return filled
                status = fill_data.get("status", "")
                if status in ("REJECTED", "CANCELLED", "CANCELED"):
                    logger.info(f"FR GRVT order {status}")
                    return ZERO

            # REST poll
            try:
                post_pos = await self.grvt_client.get_position()
                diff = post_pos - pre_pos
                if side == "buy":
                    filled = max(diff, ZERO)
                else:
                    filled = max(-diff, ZERO)

                if filled > ZERO:
                    filled = min(filled, self.config.order_quantity)
                    logger.info(f"FR GRVT fill via REST: filled={filled}")
                    try:
                        await asyncio.wait_for(
                            self.grvt_client.cancel_all_orders(), timeout=CANCEL_RPC_TIMEOUT,
                        )
                    except (asyncio.TimeoutError, Exception) as e:
                        logger.warning(f"FR cancel_all after REST fill: {e}")
                    self.grvt_client.clear_pending_fill(client_order_id)
                    return filled
            except Exception as e:
                logger.warning(f"FR GRVT REST poll failed: {e}")

        # Timeout
        logger.info(f"FR GRVT fill timeout after {timeout}s, canceling")
        try:
            await asyncio.wait_for(
                self.grvt_client.cancel_all_orders(), timeout=CANCEL_RPC_TIMEOUT,
            )
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning(f"FR cancel_all on timeout: {e}")

        await asyncio.sleep(0.2)

        # Late fill check
        fill_data = self.grvt_client.get_last_fill(client_order_id)
        if fill_data and fill_data.get("filled_size", ZERO) > ZERO:
            filled = min(fill_data["filled_size"], self.config.order_quantity)
            logger.info(f"FR GRVT late fill: {filled}")
            return filled

        self.grvt_client.clear_pending_fill(client_order_id)

        # Final REST snapshot
        try:
            post_pos = await self.grvt_client.get_position()
            diff = post_pos - pre_pos
            if side == "buy":
                filled = max(diff, ZERO)
            else:
                filled = max(-diff, ZERO)
            if filled > ZERO:
                filled = min(filled, self.config.order_quantity)
                logger.info(f"FR GRVT fill via final snapshot: {filled}")
                return filled
        except Exception as e:
            logger.warning(f"FR final snapshot failed: {e}")

        return ZERO

    async def _wait_lighter_fill(
        self,
        client_order_index: int,
        pre_pos: Decimal,
        side: str,
        max_qty: Decimal,
    ) -> Decimal:
        """Wait for Lighter IOC fill via WS + REST."""
        deadline = time.time() + LIGHTER_FILL_TIMEOUT

        while time.time() < deadline:
            remaining = max(deadline - time.time(), 0.01)
            ws_wait = min(REST_POLL_INTERVAL, remaining)

            fill_data = await self.lighter_client.wait_for_fill(
                client_order_index, timeout=ws_wait, keep_pending_on_timeout=True,
            )
            if fill_data is not None:
                filled = fill_data.get("filled_size", ZERO)
                if filled > ZERO:
                    filled = min(filled, max_qty)
                    logger.info(f"FR Lighter fill via WS: {filled}")
                    self.lighter_client._clear_pending_fill(client_order_index)
                    return filled

            try:
                post_pos = await self.lighter_client.get_position()
                diff = post_pos - pre_pos
                if side == "buy":
                    filled = max(diff, ZERO)
                else:
                    filled = max(-diff, ZERO)

                if filled > ZERO:
                    filled = min(filled, max_qty)
                    logger.info(f"FR Lighter fill via REST: {filled}")
                    self.lighter_client._clear_pending_fill(client_order_index)
                    return filled
            except Exception as e:
                logger.warning(f"FR Lighter REST poll failed: {e}")

        self.lighter_client._clear_pending_fill(client_order_index)

        # Final retries
        for attempt in range(3):
            try:
                post_pos = await self.lighter_client.get_position()
                diff = post_pos - pre_pos
                if side == "buy":
                    filled = max(diff, ZERO)
                else:
                    filled = max(-diff, ZERO)
                if filled > ZERO:
                    filled = min(filled, max_qty)
                    logger.info(f"FR Lighter fill via snapshot attempt {attempt + 1}: {filled}")
                    return filled
            except Exception as e:
                logger.warning(f"FR Lighter snapshot attempt {attempt + 1} failed: {e}")
            await asyncio.sleep(0.5)

        return ZERO

    # ========== Helpers ==========

    async def _refresh_positions(self):
        try:
            grvt_pos = await self.grvt_client.get_position()
            self.positions.grvt_position = grvt_pos
        except Exception as e:
            logger.warning(f"FR: Failed to refresh GRVT position: {e}")
        try:
            lighter_pos = await self.lighter_client.get_position()
            self.positions.lighter_position = lighter_pos
        except Exception as e:
            logger.warning(f"FR: Failed to refresh Lighter position: {e}")

    async def _check_balance(self):
        try:
            grvt_balance = await self.grvt_client.get_balance()
            lighter_balance = await self.lighter_client.get_balance()
            min_bal = min(grvt_balance, lighter_balance)

            if min_bal < MIN_BALANCE:
                if self._low_balance_first_check is None:
                    self._low_balance_first_check = time.time()
                    logger.warning(f"FR: Low balance: GRVT={grvt_balance} Lighter={lighter_balance}")
                elif time.time() - self._low_balance_first_check >= 3:
                    logger.error(f"FR: Balance confirmed low")
                    self.request_stop("INSUFFICIENT_BALANCE")
            else:
                self._low_balance_first_check = None
        except Exception as e:
            logger.warning(f"FR: Balance check failed: {e}")

    def _log_fr_data(self):
        try:
            self._fr_csv_writer.writerow([
                datetime.now(timezone.utc).isoformat(),
                str(self._current_grvt_rate), str(self._current_lighter_rate),
                str(self._rate_diff), self._state,
                self._position_direction or "",
                str(self.positions.grvt_position), str(self.positions.lighter_position),
            ])
            self._fr_csv_file.flush()
        except Exception:
            pass

    # ========== Shutdown ==========

    async def shutdown(self):
        logger.info("=== FR SHUTDOWN STARTED ===")
        self._stop_flag = True

        # Cancel all orders (parallel)
        cancel_tasks = [
            asyncio.wait_for(self.grvt_client.cancel_all_orders(), timeout=5),
            asyncio.wait_for(self.lighter_client.cancel_all_orders(), timeout=5),
        ]
        results = await asyncio.gather(*cancel_tasks, return_exceptions=True)
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                label = "GRVT" if i == 0 else "Lighter"
                logger.warning(f"FR {label} cancel all failed: {r}")

        await asyncio.sleep(0.3)

        # Close positions
        logger.info("FR shutdown: Closing positions...")
        await self._close_all_positions()

        # Disconnect
        try:
            await self.grvt_client.disconnect()
        except Exception as e:
            logger.warning(f"FR GRVT disconnect error: {e}")
        try:
            await self.lighter_client.disconnect()
        except Exception as e:
            logger.warning(f"FR Lighter disconnect error: {e}")

        # Cleanup
        runtime = (time.time() - self._start_time) / 3600
        await self.telegram.send_message(
            f"*FR Strategy Stopped*\n"
            f"Reason: {self._stop_reason}\n"
            f"Runtime: {runtime:.1f}h | Trades: {self._fr_trades}"
        )
        await self.telegram.close()
        self.data_logger.close()

        try:
            if self._fr_csv_file and not self._fr_csv_file.closed:
                self._fr_csv_file.close()
        except Exception:
            pass

        logger.info("=== FR SHUTDOWN COMPLETE ===")

    async def _close_all_positions(self):
        """Close positions with retry + escalating slippage."""
        min_size = self.config.order_quantity * Decimal("0.01")

        for attempt, slippage in enumerate(SHUTDOWN_SLIPPAGE_LEVELS):
            logger.info(f"FR close attempt {attempt + 1}/{len(SHUTDOWN_SLIPPAGE_LEVELS)}, slippage={slippage * 100}%")

            grvt_pos, lighter_pos = await asyncio.gather(
                self._safe_get_position(self.grvt_client),
                self._safe_get_position(self.lighter_client),
            )

            grvt_close = self._pick_position(grvt_pos, self.positions.grvt_position, "GRVT")
            lighter_close = self._pick_position(lighter_pos, self.positions.lighter_position, "Lighter")

            if abs(grvt_close) < min_size and abs(lighter_close) < min_size:
                logger.info("FR: Both positions cleared!")
                return

            close_tasks = []
            if abs(grvt_close) >= min_size:
                close_tasks.append(("GRVT", grvt_close, asyncio.wait_for(
                    self.grvt_client.close_position("", grvt_close, slippage),
                    timeout=10,
                )))
            if abs(lighter_close) >= min_size:
                close_tasks.append(("Lighter", lighter_close, asyncio.wait_for(
                    self.lighter_client.close_position("", lighter_close, slippage),
                    timeout=10,
                )))

            if close_tasks:
                results = await asyncio.gather(
                    *[t[2] for t in close_tasks], return_exceptions=True,
                )
                for (label, size, _), result in zip(close_tasks, results):
                    if isinstance(result, Exception):
                        logger.error(f"FR {label} close failed: {result}")
                    elif result:
                        logger.info(f"FR {label} position closed: {size}")

            await asyncio.sleep(1)

        # Final check
        final_grvt = await self._final_position_check(
            self.grvt_client, self.positions.grvt_position, "GRVT",
        )
        final_lighter = await self._final_position_check(
            self.lighter_client, self.positions.lighter_position, "Lighter",
        )

        if abs(final_grvt) >= min_size or abs(final_lighter) >= min_size:
            msg = f"FR POSITIONS NOT CLOSED! GRVT={final_grvt} Lighter={final_lighter}"
            logger.error(msg)
            await self.telegram.notify_emergency(msg)

    async def _safe_get_position(self, client) -> Decimal:
        for attempt in range(3):
            try:
                return await client.get_position()
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(1)
                    continue
                logger.error(f"FR: Failed to get position after 3 attempts: {e}")
                return ZERO
        return ZERO

    async def _final_position_check(self, client, local_pos: Decimal, label: str) -> Decimal:
        api_pos = await self._safe_get_position(client)
        if api_pos != ZERO:
            return api_pos
        if local_pos == ZERO:
            return ZERO
        logger.warning(f"FR {label} final check: API=0 but local={local_pos}, using local")
        return local_pos

    @staticmethod
    def _pick_position(api_pos: Decimal, local_pos: Decimal, label: str) -> Decimal:
        if abs(api_pos) >= abs(local_pos):
            return api_pos
        logger.warning(f"FR {label}: using local pos {local_pos} (larger than API {api_pos})")
        return local_pos
