"""
Mean Reversion Strategy — spread z-score based entry/exit.

Signal: z-score of (grvt_mid - lighter_mid) spread.
Execution: GRVT maker (post-only) + Lighter taker (IOC).
Interface matches ArbStrategy: initialize(), run(), shutdown(), request_stop().
"""

import asyncio
import csv
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from config import Config
from exchanges.grvt_client import GrvtClient
from exchanges.lighter_client import LighterClient
from helpers.logger import DataLogger
from helpers.telegram import TelegramNotifier
from strategy.order_book_manager import OrderBookManager
from strategy.position_tracker import PositionTracker

logger = logging.getLogger("arbitrage.mr")

# Constants
ZERO = Decimal("0")
LOOP_INTERVAL_MS = 50
BALANCE_CHECK_INTERVAL = 10  # seconds
HEARTBEAT_INTERVAL = 300  # seconds (5 minutes)
MIN_BALANCE = Decimal("10")
STATUS_LOG_INTERVAL = 30  # loop iterations (~1.5s)
SHUTDOWN_SLIPPAGE_LEVELS = [Decimal("0.05"), Decimal("0.10"), Decimal("0.20")]
REST_POLL_INTERVAL = 0.5  # seconds
CANCEL_RPC_TIMEOUT = 2.0  # seconds
LIGHTER_FILL_TIMEOUT = 2  # seconds (IOC fills instantly)
RECONCILE_INTERVAL = 60  # seconds


class MeanReversionStrategy:
    """
    Spread mean-reversion strategy.

    Execution: GRVT maker (post-only) + Lighter taker (IOC).
    Same maker_taker mode as existing ArbStrategy, different signal source.
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

        # MR-specific state
        self._spread_history: deque = deque(maxlen=config.mr_window)
        self._state: str = "flat"  # flat / positioned
        self._spread_direction: Optional[str] = None  # "short_spread" / "long_spread"
        self._mean: Decimal = ZERO
        self._std: Decimal = ZERO
        self._z_score: Decimal = ZERO
        self._update_count: int = 0
        self._warmed_up: bool = False
        self._entry_z: Decimal = ZERO  # z at entry (for logging)
        self._mr_trades: int = 0
        self._executing: bool = False
        self._trading_halted: bool = False
        self._halt_reason: str = ""

        # Timers
        self._last_balance_check: float = 0
        self._last_heartbeat: float = 0
        self._low_balance_first_check: Optional[float] = None
        self._last_reconcile: float = 0

        # CSV logger for MR data
        data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(data_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._mr_csv_path = os.path.join(data_dir, f"mr_{config.ticker}_{ts}.csv")
        self._mr_csv_file = open(self._mr_csv_path, "w", newline="", encoding="utf-8")
        self._mr_csv_writer = csv.writer(self._mr_csv_file)
        self._mr_csv_writer.writerow([
            "timestamp", "spread", "mean", "std", "z_score", "state",
            "direction", "grvt_pos", "lighter_pos",
        ])

    # ========== Interface ==========

    async def initialize(self) -> None:
        logger.info("Initializing MR strategy...")
        logger.info(
            f"MR Config: ticker={self.config.ticker} size={self.config.order_quantity} "
            f"max_pos={self.config.max_position} window={self.config.mr_window} "
            f"entry_z={self.config.mr_entry_z} exit_z={self.config.mr_exit_z} "
            f"stop_z={self.config.mr_stop_z} maker_timeout={self.config.mr_maker_timeout}s "
            f"warmup={self.config.mr_warmup}"
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

        # If already positioned, resume
        if self.positions.grvt_position != ZERO or self.positions.lighter_position != ZERO:
            self._state = "positioned"
            # Infer direction from position signs
            if self.positions.grvt_position < ZERO:
                self._spread_direction = "short_spread"  # sold GRVT
            elif self.positions.grvt_position > ZERO:
                self._spread_direction = "long_spread"  # bought GRVT
            logger.info(f"Resuming in POSITIONED state: direction={self._spread_direction}")

        await self.telegram.send_message(
            f"*MR Strategy Started*\n"
            f"Pair: {self.config.ticker} | Size: {self.config.order_quantity}\n"
            f"Window: {self.config.mr_window} | Entry z: {self.config.mr_entry_z} | "
            f"Exit z: {self.config.mr_exit_z} | Stop z: {self.config.mr_stop_z}"
        )

        self._start_time = time.time()
        logger.info("MR strategy initialized")

    async def run(self) -> None:
        logger.info("MR: Starting main loop (50ms/iteration)")
        loop_count = 0

        while not self._stop_flag:
            try:
                await self._main_loop_iteration(loop_count)
            except Exception as e:
                logger.error(f"MR loop iteration error: {e}", exc_info=True)

            loop_count += 1
            await asyncio.sleep(LOOP_INTERVAL_MS / 1000)

        logger.info(f"MR main loop exited: {self._stop_reason}")

    def request_stop(self, reason: str = "user interrupt"):
        self._stop_flag = True
        self._stop_reason = reason
        logger.info(f"MR stop requested: {reason}")

    # ========== Main Loop ==========

    async def _main_loop_iteration(self, loop_count: int):
        now = time.time()

        # 1. WS health check
        if self.grvt_client.is_ws_stale():
            if loop_count % 200 == 0:
                logger.warning("MR: GRVT WS stale, skipping")
            return
        if self.lighter_client.is_ws_stale():
            if loop_count % 200 == 0:
                logger.warning("MR: Lighter WS stale, skipping")
            return
        if not self.lighter_client.is_orderbook_ready():
            if loop_count % 200 == 0:
                logger.warning("MR: Lighter OB not ready")
            return

        # 2. Refresh BBO
        grvt_bid, grvt_ask = self.ob_manager.get_grvt_bbo()
        lighter_bid, lighter_ask = self.ob_manager.get_lighter_bbo()

        if any(v is None for v in (grvt_bid, grvt_ask, lighter_bid, lighter_ask)):
            return

        # 3. Compute spread
        grvt_mid = (grvt_bid + grvt_ask) / 2
        lighter_mid = (lighter_bid + lighter_ask) / 2
        spread = grvt_mid - lighter_mid
        self._spread_history.append(spread)
        self._update_count += 1

        # 4. Every 20 iterations (~1s) recompute mean/std
        if self._update_count % 20 == 0:
            self._recompute_stats()

        # 5. Warmup check
        if not self._warmed_up:
            if self._update_count >= self.config.mr_warmup * 20:
                self._warmed_up = True
                logger.info(
                    f"MR warmup complete: mean=${self._mean:.2f} std=${self._std:.2f} "
                    f"samples={len(self._spread_history)}"
                )
            elif loop_count % 100 == 0:
                logger.info(
                    f"MR warming up: {self._update_count}/{self.config.mr_warmup * 20} "
                    f"ticks ({len(self._spread_history)} samples)"
                )
            return

        # 6. z-score (skip if std too small)
        if self._std < Decimal("0.01"):
            return
        self._z_score = (spread - self._mean) / self._std

        # 7. CSV log (~1s)
        if self._update_count % 20 == 0:
            self._log_mr_data(spread)

        # 8. Periodic status log (~1.5s)
        if loop_count % STATUS_LOG_INTERVAL == 0 and loop_count > 0:
            logger.info(
                f"MR z={self._z_score:.2f} mean=${self._mean:.2f} std=${self._std:.2f} "
                f"spread=${spread:.2f} state={self._state} dir={self._spread_direction} "
                f"trades={self._mr_trades} "
                f"pos: G={self.positions.grvt_position} L={self.positions.lighter_position}"
            )

        # 9. Trading halt check
        if self._trading_halted:
            if loop_count % 200 == 0:
                logger.warning(f"MR trading halted: {self._halt_reason}")
            self.request_stop(f"MR_{self._halt_reason}")
            return

        # 10. State machine
        if not self._executing:
            if self._state == "flat":
                await self._handle_flat()
            elif self._state == "positioned":
                await self._handle_positioned()

        # 11. Risk check
        if not self.positions.check_risk():
            self.request_stop("POSITION DIVERGENCE")
            await self.telegram.notify_emergency(
                f"MR Position divergence! "
                f"GRVT={self.positions.grvt_position} "
                f"Lighter={self.positions.lighter_position}"
            )
            return

        # 12. Position reconciliation
        if now - self._last_reconcile >= RECONCILE_INTERVAL:
            self._last_reconcile = now
            await self.positions.reconcile_with_api(self.grvt_client, self.lighter_client)

        # 13. Balance check
        if now - self._last_balance_check >= BALANCE_CHECK_INTERVAL:
            self._last_balance_check = now
            await self._check_balance()

        # 14. Heartbeat
        if now - self._last_heartbeat >= HEARTBEAT_INTERVAL:
            self._last_heartbeat = now
            await self.telegram.send_message(
                f"*MR Heartbeat* | Runtime {(now - self._start_time) / 3600:.1f}h | "
                f"Trades: {self._mr_trades}\n"
                f"z={self._z_score:.2f} mean=${self._mean:.2f} std=${self._std:.2f}\n"
                f"State: {self._state} dir={self._spread_direction}\n"
                f"GRVT: {self.positions.grvt_position} | Lighter: {self.positions.lighter_position}"
            )

    # ========== Statistics ==========

    def _recompute_stats(self):
        """Recompute rolling mean and std from spread history."""
        if len(self._spread_history) < 2:
            return
        n = len(self._spread_history)
        total = sum(self._spread_history)
        self._mean = total / n
        variance = sum((x - self._mean) ** 2 for x in self._spread_history) / (n - 1)
        # Decimal sqrt via Newton's method
        self._std = self._decimal_sqrt(variance)

    @staticmethod
    def _decimal_sqrt(value: Decimal) -> Decimal:
        """Newton's method sqrt for Decimal."""
        if value <= ZERO:
            return ZERO
        # Initial guess
        x = value
        for _ in range(50):
            x_new = (x + value / x) / 2
            if abs(x_new - x) < Decimal("1E-20"):
                break
            x = x_new
        return x

    # ========== Signal Handlers ==========

    async def _handle_flat(self):
        z = self._z_score

        if z > self.config.mr_entry_z:
            # Spread above mean → short spread: sell GRVT, buy Lighter
            logger.info(f"MR SIGNAL: SHORT spread z={z:.2f} > {self.config.mr_entry_z}")
            await self._execute_entry("short_spread")

        elif z < -self.config.mr_entry_z:
            # Spread below mean → long spread: buy GRVT, sell Lighter
            logger.info(f"MR SIGNAL: LONG spread z={z:.2f} < -{self.config.mr_entry_z}")
            await self._execute_entry("long_spread")

    async def _handle_positioned(self):
        z = self._z_score

        # Exit: z reverted
        should_exit = False
        if self._spread_direction == "short_spread":
            should_exit = z < self.config.mr_exit_z
        else:  # long_spread
            should_exit = z > -self.config.mr_exit_z

        # Stop-loss: z diverged further
        should_stop = abs(z) > self.config.mr_stop_z

        if should_exit:
            logger.info(f"MR EXIT: z={z:.2f} reverted (entry_z={self._entry_z:.2f})")
            await self._execute_exit()
        elif should_stop:
            logger.warning(f"MR STOP-LOSS: z={z:.2f} > {self.config.mr_stop_z}")
            await self._execute_exit()

    # ========== Execution ==========

    async def _execute_entry(self, direction: str):
        """GRVT maker → wait fill → Lighter taker. Same as maker_taker mode."""
        if self._executing:
            return
        self._executing = True

        try:
            if direction == "short_spread":
                grvt_side, lighter_side = "sell", "buy"
            else:
                grvt_side, lighter_side = "buy", "sell"

            # Capacity check
            if grvt_side == "buy" and not self.positions.can_long_grvt():
                logger.info("MR: Max long position reached, skipping")
                return
            if grvt_side == "sell" and not self.positions.can_short_grvt():
                logger.info("MR: Max short position reached, skipping")
                return

            # Phase 0: Refresh positions from API
            await self._refresh_positions()
            pre_pos_grvt = self.positions.grvt_position

            if not self.positions.check_pre_trade():
                logger.warning("MR: Pre-trade position check failed, skipping")
                return

            # Phase 1: GRVT post-only order
            grvt_bid, grvt_ask = self.grvt_client.get_bbo()
            if grvt_bid is None or grvt_ask is None:
                logger.warning("MR: GRVT BBO not available, aborting")
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
                logger.error(f"MR GRVT place order failed: {e}")
                return

            # Phase 2: Wait for GRVT fill (WS + REST concurrent)
            logger.info(f"MR Phase 2: Waiting for GRVT fill (timeout={self.config.mr_maker_timeout}s)")
            filled_qty = await self._wait_grvt_fill(
                grvt_cid, pre_pos_grvt, grvt_side, price,
                timeout=self.config.mr_maker_timeout,
            )

            if filled_qty <= ZERO:
                logger.info("MR GRVT maker not filled, back to FLAT")
                try:
                    await asyncio.wait_for(
                        self.grvt_client.cancel_all_orders(), timeout=CANCEL_RPC_TIMEOUT,
                    )
                except (asyncio.TimeoutError, Exception) as e:
                    logger.warning(f"MR cancel_all after no fill: {e}")
                return

            # Phase 3: Lighter IOC hedge
            pre_pos_lighter = self.positions.lighter_position
            logger.info(f"MR Phase 3: Lighter IOC {lighter_side} {filled_qty}")
            try:
                lighter_idx = await self.lighter_client.place_ioc_order(
                    side=lighter_side, size=filled_qty,
                )
            except Exception as e:
                # CRITICAL: GRVT filled but Lighter submit failed
                logger.error(
                    f"MR LIGHTER HEDGE FAILED! GRVT {grvt_side} {filled_qty} filled, "
                    f"Lighter {lighter_side} error: {e}"
                )
                self.positions.update_grvt(grvt_side, filled_qty)
                self._trading_halted = True
                self._halt_reason = "HEDGE_SUBMIT_FAILED"
                await self.telegram.notify_emergency(
                    f"MR HEDGE FAILURE: GRVT {grvt_side} {filled_qty} filled, "
                    f"Lighter {lighter_side} failed: {e}"
                )
                return

            # Phase 4: Wait for Lighter fill
            lighter_filled = await self._wait_lighter_fill(
                lighter_idx, pre_pos_lighter, lighter_side, filled_qty,
            )

            if lighter_filled <= ZERO:
                # Lighter didn't fill → halt
                self.positions.update_grvt(grvt_side, filled_qty)
                logger.error(f"MR LIGHTER FILL UNKNOWN! GRVT {grvt_side} {filled_qty} filled")
                self._trading_halted = True
                self._halt_reason = "HEDGE_FILL_UNKNOWN"
                await self.telegram.notify_emergency(
                    f"MR LIGHTER FILL TIMEOUT: GRVT {grvt_side} {filled_qty} confirmed, "
                    f"Lighter {lighter_side} fill unknown"
                )
                return

            # Phase 5: Update positions, enter POSITIONED
            self.positions.update_grvt(grvt_side, filled_qty)
            self.positions.update_lighter(lighter_side, lighter_filled)
            self._state = "positioned"
            self._spread_direction = direction
            self._entry_z = self._z_score
            self._mr_trades += 1

            logger.info(
                f"MR POSITIONED: {direction} z={self._z_score:.2f} "
                f"GRVT {grvt_side} {filled_qty} / Lighter {lighter_side} {lighter_filled} "
                f"pos: G={self.positions.grvt_position} L={self.positions.lighter_position}"
            )

            # Log trade
            self.data_logger.log_trade(
                direction=f"mr_{direction}",
                grvt_side=grvt_side, grvt_price=price, grvt_size=filled_qty,
                lighter_side=lighter_side, lighter_price=ZERO, lighter_size=lighter_filled,
                spread=self._z_score,
                grvt_position=self.positions.grvt_position,
                lighter_position=self.positions.lighter_position,
            )

            await self.telegram.send_message(
                f"*MR Entry: {direction}*\n"
                f"z={self._z_score:.2f} | GRVT {grvt_side} {filled_qty} | "
                f"Lighter {lighter_side} {lighter_filled}\n"
                f"Pos: G={self.positions.grvt_position} L={self.positions.lighter_position}"
            )

        except Exception as e:
            logger.error(f"MR execute_entry unexpected error: {e}", exc_info=True)
        finally:
            self._executing = False

    async def _execute_exit(self):
        """Reverse position: GRVT maker + Lighter taker."""
        if self._executing:
            return
        self._executing = True

        try:
            if self._spread_direction == "short_spread":
                # Was: sell GRVT, buy Lighter → Exit: buy GRVT, sell Lighter
                grvt_side, lighter_side = "buy", "sell"
            else:
                # Was: buy GRVT, sell Lighter → Exit: sell GRVT, buy Lighter
                grvt_side, lighter_side = "sell", "buy"

            # Phase 0: Refresh positions
            await self._refresh_positions()
            pre_pos_grvt = self.positions.grvt_position

            # Phase 1: GRVT post-only order
            grvt_bid, grvt_ask = self.grvt_client.get_bbo()
            if grvt_bid is None or grvt_ask is None:
                logger.warning("MR exit: GRVT BBO not available")
                return

            tick = self.grvt_client.tick_size
            if grvt_side == "buy":
                price = grvt_ask - tick
            else:
                price = grvt_bid + tick

            # Exit size = current GRVT position (absolute)
            exit_size = abs(self.positions.grvt_position)
            if exit_size <= ZERO:
                logger.info("MR exit: no position to close, back to FLAT")
                self._state = "flat"
                self._spread_direction = None
                return

            # Cap at order_quantity
            exit_size = min(exit_size, self.config.order_quantity)

            try:
                grvt_cid = await self.grvt_client.place_post_only_order(
                    grvt_side, price, exit_size,
                )
            except Exception as e:
                logger.error(f"MR exit: GRVT place order failed: {e}")
                return

            # Phase 2: Wait for GRVT fill
            logger.info(f"MR exit Phase 2: Waiting for GRVT fill (timeout={self.config.mr_maker_timeout}s)")
            filled_qty = await self._wait_grvt_fill(
                grvt_cid, pre_pos_grvt, grvt_side, price,
                timeout=self.config.mr_maker_timeout,
            )

            if filled_qty <= ZERO:
                logger.info("MR exit: GRVT maker not filled, staying POSITIONED")
                try:
                    await asyncio.wait_for(
                        self.grvt_client.cancel_all_orders(), timeout=CANCEL_RPC_TIMEOUT,
                    )
                except (asyncio.TimeoutError, Exception) as e:
                    logger.warning(f"MR exit cancel_all: {e}")
                return

            # Phase 3: Lighter IOC
            pre_pos_lighter = self.positions.lighter_position
            logger.info(f"MR exit Phase 3: Lighter IOC {lighter_side} {filled_qty}")
            try:
                lighter_idx = await self.lighter_client.place_ioc_order(
                    side=lighter_side, size=filled_qty,
                )
            except Exception as e:
                logger.error(
                    f"MR EXIT LIGHTER HEDGE FAILED! GRVT {grvt_side} {filled_qty}, "
                    f"Lighter {lighter_side} error: {e}"
                )
                self.positions.update_grvt(grvt_side, filled_qty)
                self._trading_halted = True
                self._halt_reason = "EXIT_HEDGE_SUBMIT_FAILED"
                await self.telegram.notify_emergency(
                    f"MR EXIT HEDGE FAILURE: GRVT {grvt_side} {filled_qty}, "
                    f"Lighter {lighter_side} failed: {e}"
                )
                return

            # Phase 4: Wait for Lighter fill
            lighter_filled = await self._wait_lighter_fill(
                lighter_idx, pre_pos_lighter, lighter_side, filled_qty,
            )

            if lighter_filled <= ZERO:
                self.positions.update_grvt(grvt_side, filled_qty)
                logger.error(f"MR EXIT LIGHTER FILL UNKNOWN! GRVT {grvt_side} {filled_qty}")
                self._trading_halted = True
                self._halt_reason = "EXIT_HEDGE_FILL_UNKNOWN"
                await self.telegram.notify_emergency(
                    f"MR EXIT LIGHTER TIMEOUT: GRVT {grvt_side} {filled_qty}, "
                    f"Lighter {lighter_side} fill unknown"
                )
                return

            # Phase 5: Update positions, back to FLAT
            self.positions.update_grvt(grvt_side, filled_qty)
            self.positions.update_lighter(lighter_side, lighter_filled)
            self._mr_trades += 1

            logger.info(
                f"MR FLAT: position closed z={self._z_score:.2f} "
                f"GRVT {grvt_side} {filled_qty} / Lighter {lighter_side} {lighter_filled} "
                f"pos: G={self.positions.grvt_position} L={self.positions.lighter_position}"
            )

            # Check if fully closed
            if abs(self.positions.grvt_position) < self.config.order_quantity * Decimal("0.01"):
                self._state = "flat"
                self._spread_direction = None
            else:
                logger.info(
                    f"MR: Partial exit, remaining pos G={self.positions.grvt_position}, staying POSITIONED"
                )

            self.data_logger.log_trade(
                direction=f"mr_exit_{self._spread_direction or 'unknown'}",
                grvt_side=grvt_side, grvt_price=price, grvt_size=filled_qty,
                lighter_side=lighter_side, lighter_price=ZERO, lighter_size=lighter_filled,
                spread=self._z_score,
                grvt_position=self.positions.grvt_position,
                lighter_position=self.positions.lighter_position,
            )

            await self.telegram.send_message(
                f"*MR Exit*\n"
                f"z={self._z_score:.2f} | GRVT {grvt_side} {filled_qty} | "
                f"Lighter {lighter_side} {lighter_filled}\n"
                f"Pos: G={self.positions.grvt_position} L={self.positions.lighter_position}"
            )

        except Exception as e:
            logger.error(f"MR execute_exit unexpected error: {e}", exc_info=True)
        finally:
            self._executing = False

    # ========== Fill Detection (WS + REST concurrent) ==========

    async def _wait_grvt_fill(
        self,
        client_order_id: str,
        pre_pos: Decimal,
        side: str,
        price: Decimal,
        timeout: float,
    ) -> Decimal:
        """Wait for GRVT fill via concurrent WS + REST polling.
        Returns filled_qty (capped at order_quantity), or ZERO on timeout."""
        deadline = time.time() + timeout

        while time.time() < deadline:
            remaining = max(deadline - time.time(), 0.01)
            ws_wait = min(REST_POLL_INTERVAL, remaining)

            # Short WS wait
            fill_data = await self.grvt_client.wait_for_fill(
                client_order_id, timeout=ws_wait, keep_pending_on_timeout=True,
            )
            if fill_data is not None:
                filled = fill_data.get("filled_size", ZERO)
                if filled > ZERO:
                    filled = min(filled, self.config.order_quantity)
                    logger.info(f"MR GRVT fill via WS: {filled}")
                    return filled
                # REJECTED/CANCELLED with zero fill
                status = fill_data.get("status", "")
                if status in ("REJECTED", "CANCELLED", "CANCELED"):
                    logger.info(f"MR GRVT order {status}")
                    return ZERO

            # REST position poll
            try:
                post_pos = await self.grvt_client.get_position()
                diff = post_pos - pre_pos
                if side == "buy":
                    filled = max(diff, ZERO)
                else:
                    filled = max(-diff, ZERO)

                if filled > ZERO:
                    filled = min(filled, self.config.order_quantity)
                    logger.info(
                        f"MR GRVT fill via REST: pre={pre_pos} post={post_pos} filled={filled}"
                    )
                    # Cancel remaining orders
                    try:
                        await asyncio.wait_for(
                            self.grvt_client.cancel_all_orders(), timeout=CANCEL_RPC_TIMEOUT,
                        )
                    except (asyncio.TimeoutError, Exception) as e:
                        logger.warning(f"MR cancel_all after REST fill: {e}")
                    self.grvt_client.clear_pending_fill(client_order_id)
                    return filled
            except Exception as e:
                logger.warning(f"MR GRVT REST poll failed: {e}")

        # Timeout — cancel all and do final snapshot check
        logger.info(f"MR GRVT fill timeout after {timeout}s, canceling")
        try:
            await asyncio.wait_for(
                self.grvt_client.cancel_all_orders(), timeout=CANCEL_RPC_TIMEOUT,
            )
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning(f"MR cancel_all on timeout: {e}")

        await asyncio.sleep(0.2)  # settle

        # Check for late fill
        fill_data = self.grvt_client.get_last_fill(client_order_id)
        if fill_data and fill_data.get("filled_size", ZERO) > ZERO:
            filled = min(fill_data["filled_size"], self.config.order_quantity)
            logger.info(f"MR GRVT late fill after cancel: {filled}")
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
                logger.info(f"MR GRVT fill via final snapshot: {filled}")
                return filled
        except Exception as e:
            logger.warning(f"MR final snapshot failed: {e}")

        return ZERO

    async def _wait_lighter_fill(
        self,
        client_order_index: int,
        pre_pos: Decimal,
        side: str,
        max_qty: Decimal,
    ) -> Decimal:
        """Wait for Lighter IOC fill via WS + REST concurrent race.
        Returns filled_qty or ZERO."""
        deadline = time.time() + LIGHTER_FILL_TIMEOUT

        while time.time() < deadline:
            remaining = max(deadline - time.time(), 0.01)
            ws_wait = min(REST_POLL_INTERVAL, remaining)

            # Short WS wait
            fill_data = await self.lighter_client.wait_for_fill(
                client_order_index, timeout=ws_wait, keep_pending_on_timeout=True,
            )
            if fill_data is not None:
                filled = fill_data.get("filled_size", ZERO)
                if filled > ZERO:
                    filled = min(filled, max_qty)
                    logger.info(f"MR Lighter fill via WS: {filled}")
                    self.lighter_client._clear_pending_fill(client_order_index)
                    return filled

            # REST position poll
            try:
                post_pos = await self.lighter_client.get_position()
                diff = post_pos - pre_pos
                if side == "buy":
                    filled = max(diff, ZERO)
                else:
                    filled = max(-diff, ZERO)

                if filled > ZERO:
                    filled = min(filled, max_qty)
                    logger.info(
                        f"MR Lighter fill via REST: pre={pre_pos} post={post_pos} filled={filled}"
                    )
                    self.lighter_client._clear_pending_fill(client_order_index)
                    return filled
            except Exception as e:
                logger.warning(f"MR Lighter REST poll failed: {e}")

        # Timeout — final REST snapshot with retries
        self.lighter_client._clear_pending_fill(client_order_index)
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
                    logger.info(f"MR Lighter fill via snapshot attempt {attempt + 1}: {filled}")
                    return filled
            except Exception as e:
                logger.warning(f"MR Lighter snapshot attempt {attempt + 1} failed: {e}")
            await asyncio.sleep(0.5)

        return ZERO

    # ========== Helpers ==========

    async def _refresh_positions(self):
        """Refresh positions from API."""
        try:
            grvt_pos = await self.grvt_client.get_position()
            self.positions.grvt_position = grvt_pos
        except Exception as e:
            logger.warning(f"MR: Failed to refresh GRVT position: {e}")

        try:
            lighter_pos = await self.lighter_client.get_position()
            self.positions.lighter_position = lighter_pos
        except Exception as e:
            logger.warning(f"MR: Failed to refresh Lighter position: {e}")

        logger.debug(
            f"MR positions refreshed: GRVT={self.positions.grvt_position} "
            f"Lighter={self.positions.lighter_position}"
        )

    async def _check_balance(self):
        """Double-confirm balance check."""
        try:
            grvt_balance = await self.grvt_client.get_balance()
            lighter_balance = await self.lighter_client.get_balance()
            min_bal = min(grvt_balance, lighter_balance)

            if min_bal < MIN_BALANCE:
                if self._low_balance_first_check is None:
                    self._low_balance_first_check = time.time()
                    logger.warning(f"MR: Low balance: GRVT={grvt_balance} Lighter={lighter_balance}")
                elif time.time() - self._low_balance_first_check >= 3:
                    logger.error(f"MR: Balance confirmed low: GRVT={grvt_balance} Lighter={lighter_balance}")
                    self.request_stop("INSUFFICIENT_BALANCE")
            else:
                self._low_balance_first_check = None
        except Exception as e:
            logger.warning(f"MR: Balance check failed: {e}")

    def _log_mr_data(self, spread: Decimal):
        """Write MR CSV row."""
        try:
            self._mr_csv_writer.writerow([
                datetime.now(timezone.utc).isoformat(),
                str(spread), str(self._mean), str(self._std), str(self._z_score),
                self._state, self._spread_direction or "",
                str(self.positions.grvt_position), str(self.positions.lighter_position),
            ])
            self._mr_csv_file.flush()
        except Exception:
            pass

    # ========== Shutdown ==========

    async def shutdown(self):
        """Graceful exit with position closing."""
        logger.info("=== MR SHUTDOWN STARTED ===")
        self._stop_flag = True

        # Step 1: Cancel all orders (parallel)
        logger.info("MR shutdown Step 1: Canceling all orders...")
        cancel_tasks = [
            asyncio.wait_for(self.grvt_client.cancel_all_orders(), timeout=5),
            asyncio.wait_for(self.lighter_client.cancel_all_orders(), timeout=5),
        ]
        results = await asyncio.gather(*cancel_tasks, return_exceptions=True)
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                label = "GRVT" if i == 0 else "Lighter"
                logger.warning(f"MR {label} cancel all failed: {r}")

        await asyncio.sleep(0.3)

        # Step 2: Close positions with escalating slippage
        logger.info("MR shutdown Step 2: Closing positions...")
        await self._close_all_positions()

        # Step 3: Disconnect
        logger.info("MR shutdown Step 3: Disconnecting...")
        try:
            await self.grvt_client.disconnect()
        except Exception as e:
            logger.warning(f"MR GRVT disconnect error: {e}")
        try:
            await self.lighter_client.disconnect()
        except Exception as e:
            logger.warning(f"MR Lighter disconnect error: {e}")

        # Step 4: Cleanup
        runtime = (time.time() - self._start_time) / 3600
        await self.telegram.send_message(
            f"*MR Strategy Stopped*\n"
            f"Reason: {self._stop_reason}\n"
            f"Runtime: {runtime:.1f}h | Trades: {self._mr_trades}"
        )
        await self.telegram.close()
        self.data_logger.close()

        try:
            if self._mr_csv_file and not self._mr_csv_file.closed:
                self._mr_csv_file.close()
        except Exception:
            pass

        logger.info("=== MR SHUTDOWN COMPLETE ===")

    async def _close_all_positions(self):
        """Close positions on both exchanges with retry + escalating slippage."""
        min_size = self.config.order_quantity * Decimal("0.01")

        for attempt, slippage in enumerate(SHUTDOWN_SLIPPAGE_LEVELS):
            logger.info(f"MR close attempt {attempt + 1}/{len(SHUTDOWN_SLIPPAGE_LEVELS)}, slippage={slippage * 100}%")

            # Fetch real positions (parallel)
            grvt_pos, lighter_pos = await asyncio.gather(
                self._safe_get_position(self.grvt_client),
                self._safe_get_position(self.lighter_client),
            )

            grvt_close = self._pick_position(grvt_pos, self.positions.grvt_position, "GRVT")
            lighter_close = self._pick_position(lighter_pos, self.positions.lighter_position, "Lighter")

            if abs(grvt_close) < min_size and abs(lighter_close) < min_size:
                logger.info("MR: Both positions cleared!")
                return

            # Close both in parallel
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
                        logger.error(f"MR {label} close failed: {result}")
                    elif result:
                        logger.info(f"MR {label} position closed: {size}")

            await asyncio.sleep(1)

        # Final check
        final_grvt = await self._final_position_check(
            self.grvt_client, self.positions.grvt_position, "GRVT",
        )
        final_lighter = await self._final_position_check(
            self.lighter_client, self.positions.lighter_position, "Lighter",
        )

        if abs(final_grvt) >= min_size or abs(final_lighter) >= min_size:
            msg = f"MR POSITIONS NOT CLOSED! GRVT={final_grvt} Lighter={final_lighter}"
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
                logger.error(f"MR: Failed to get position after 3 attempts: {e}")
                return ZERO
        return ZERO

    async def _final_position_check(self, client, local_pos: Decimal, label: str) -> Decimal:
        api_pos = await self._safe_get_position(client)
        if api_pos != ZERO:
            return api_pos
        if local_pos == ZERO:
            return ZERO
        logger.warning(f"MR {label} final check: API=0 but local={local_pos}, using local")
        return local_pos

    @staticmethod
    def _pick_position(api_pos: Decimal, local_pos: Decimal, label: str) -> Decimal:
        if abs(api_pos) >= abs(local_pos):
            return api_pos
        logger.warning(f"MR {label}: using local pos {local_pos} (larger than API {api_pos})")
        return local_pos
