"""
Main arbitrage strategy — 50ms loop with full lifecycle management.

Loop:
  1. WS health check (both sides, 30s stale threshold)
  2. Refresh BBO from both exchanges
  3. Update spread analyzer
  4. Log BBO to CSV
  5. Periodic status log (every 30 iterations = 1.5s)
  6. Heartbeat TG (every 5 minutes)
  7. Balance check (every 10s, double-confirm)
  8. Risk check (position divergence > qty*3 → emergency stop)
  9. Signal detection → execute_arb()
"""

import asyncio
import logging
import signal
import time
from decimal import Decimal
from typing import Optional

from config import Config
from exchanges.grvt_client import GrvtClient
from exchanges.lighter_client import LighterClient
from helpers.logger import DataLogger
from helpers.telegram import TelegramNotifier
from strategy.order_book_manager import OrderBookManager
from strategy.order_manager import OrderManager
from strategy.position_tracker import PositionTracker
from strategy.spread_analyzer import SpreadAnalyzer

logger = logging.getLogger("arbitrage.strategy")

# Constants
LOOP_INTERVAL_MS = 50
BALANCE_CHECK_INTERVAL = 10  # seconds
HEARTBEAT_INTERVAL = 300  # seconds (5 minutes)
MIN_BALANCE = Decimal("10")  # USDC
STATUS_LOG_INTERVAL = 30  # loop iterations (~1.5s)
SHUTDOWN_SLIPPAGE_LEVELS = [Decimal("0.02"), Decimal("0.05"), Decimal("0.10")]


class ArbStrategy:
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
        self.spread_analyzer = SpreadAnalyzer(config.long_threshold, config.short_threshold)
        self.positions = PositionTracker(config.order_quantity, config.max_position)
        self.data_logger = DataLogger(config.ticker)
        self.telegram = TelegramNotifier(config.tg_bot_token, config.tg_chat_id)

        self.order_manager = OrderManager(
            grvt_client=self.grvt_client,
            lighter_client=self.lighter_client,
            positions=self.positions,
            data_logger=self.data_logger,
            telegram=self.telegram,
            order_quantity=config.order_quantity,
            fill_timeout=config.fill_timeout,
        )

        # Timers
        self._last_balance_check: float = 0
        self._last_heartbeat: float = 0
        self._low_balance_first_check: Optional[float] = None

    async def initialize(self) -> None:
        logger.info("Initializing strategy...")
        logger.info(f"Config: ticker={self.config.ticker} size={self.config.order_quantity} "
                     f"max_pos={self.config.max_position} "
                     f"long_thresh={self.config.long_threshold} short_thresh={self.config.short_threshold}")

        await self.grvt_client.connect()
        await self.lighter_client.connect()

        # Initial position load
        try:
            self.positions.grvt_position = await self.grvt_client.get_position()
            self.positions.lighter_position = await self.lighter_client.get_position()
            logger.info(f"Initial positions: GRVT={self.positions.grvt_position} "
                        f"Lighter={self.positions.lighter_position}")
        except Exception as e:
            logger.warning(f"Failed to load initial positions: {e}")

        await self.telegram.notify_start(
            self.config.ticker, self.config.order_quantity,
            self.config.max_position, self.config.long_threshold, self.config.short_threshold,
        )

        self._start_time = time.time()
        logger.info("Strategy initialized")

    async def run(self) -> None:
        logger.info("Starting main loop (50ms/iteration)")
        loop_count = 0

        while not self._stop_flag:
            try:
                await self._main_loop_iteration(loop_count)
            except Exception as e:
                logger.error(f"Loop iteration error: {e}", exc_info=True)

            loop_count += 1
            await asyncio.sleep(LOOP_INTERVAL_MS / 1000)

        logger.info(f"Main loop exited: {self._stop_reason}")

    def request_stop(self, reason: str = "user interrupt"):
        self._stop_flag = True
        self._stop_reason = reason
        logger.info(f"Stop requested: {reason}")

    async def _main_loop_iteration(self, loop_count: int):
        now = time.time()

        # 1. WS health check
        if self.grvt_client.is_ws_stale():
            if loop_count % 200 == 0:
                logger.warning("GRVT WS stale, skipping trading")
            return
        if self.lighter_client.is_ws_stale():
            if loop_count % 200 == 0:
                logger.warning("Lighter WS stale, skipping trading")
            return
        if not self.lighter_client.is_orderbook_ready():
            if loop_count % 200 == 0:
                logger.warning("Lighter orderbook not ready")
            return

        # 2. Refresh BBO
        grvt_bid, grvt_ask = self.ob_manager.get_grvt_bbo()
        lighter_bid, lighter_ask = self.ob_manager.get_lighter_bbo()

        if any(v is None for v in (grvt_bid, grvt_ask, lighter_bid, lighter_ask)):
            return

        # 3. Update spread
        self.spread_analyzer.update(lighter_bid, lighter_ask, grvt_bid, grvt_ask)

        # 4. Log BBO to CSV (every 20 iterations = ~1s)
        if loop_count % 20 == 0:
            stats = self.spread_analyzer.get_stats()
            self.data_logger.log_bbo(
                grvt_bid, grvt_ask, lighter_bid, lighter_ask,
                stats["diff_long"], stats["diff_short"],
            )

        # 5. Periodic status log
        if loop_count % STATUS_LOG_INTERVAL == 0 and loop_count > 0:
            stats = self.spread_analyzer.get_stats()
            logger.info(
                f"BBO: GRVT={grvt_bid}/{grvt_ask} Lighter={lighter_bid}/{lighter_ask} "
                f"spread_long=${stats['diff_long']:.2f}(gap=${stats['long_gap']:.2f}) "
                f"spread_short=${stats['diff_short']:.2f}(gap=${stats['short_gap']:.2f}) "
                f"pos: G={self.positions.grvt_position} L={self.positions.lighter_position}"
            )

        # 6. Heartbeat
        if now - self._last_heartbeat >= HEARTBEAT_INTERVAL:
            self._last_heartbeat = now
            stats = self.spread_analyzer.get_stats()
            await self.telegram.notify_heartbeat(
                runtime_hours=(now - self._start_time) / 3600,
                total_trades=self.order_manager.total_trades,
                diff_long=float(stats["diff_long"]),
                diff_short=float(stats["diff_short"]),
                long_trigger=float(stats["long_threshold"]),
                short_trigger=float(stats["short_threshold"]),
                grvt_position=self.positions.grvt_position,
                lighter_position=self.positions.lighter_position,
                net_position=self.positions.get_net_position(),
            )

        # 7. Balance check (double-confirm pattern from 01-lighter)
        if now - self._last_balance_check >= BALANCE_CHECK_INTERVAL:
            self._last_balance_check = now
            await self._check_balance()

        # 8. Risk check
        if not self.positions.check_risk():
            self.request_stop("POSITION DIVERGENCE")
            await self.telegram.notify_emergency(
                f"Position divergence detected! "
                f"GRVT={self.positions.grvt_position} "
                f"Lighter={self.positions.lighter_position} "
                f"net={self.positions.get_net_position()}"
            )
            return

        # Periodic position reconciliation
        await self.positions.reconcile_with_api(self.grvt_client, self.lighter_client)

        # 9. Signal detection
        direction, spread_value = self.spread_analyzer.check_signal()
        if direction:
            logger.info(f"SIGNAL: {direction} spread=${spread_value:.4f}")
            await self.order_manager.execute_arb(direction)

    async def _check_balance(self):
        """Double-confirm balance check (01-lighter pattern)."""
        try:
            grvt_balance = await self.grvt_client.get_balance()
            lighter_balance = await self.lighter_client.get_balance()
            min_bal = min(grvt_balance, lighter_balance)

            if min_bal < MIN_BALANCE:
                if self._low_balance_first_check is None:
                    # First detection — wait and recheck (防 API 502 返回 0)
                    self._low_balance_first_check = time.time()
                    logger.warning(f"Low balance detected: GRVT={grvt_balance} Lighter={lighter_balance}, will recheck")
                elif time.time() - self._low_balance_first_check >= 3:
                    # Second confirmation after 3s
                    logger.error(f"Balance confirmed low: GRVT={grvt_balance} Lighter={lighter_balance}")
                    self.request_stop("INSUFFICIENT_BALANCE")
            else:
                self._low_balance_first_check = None
        except Exception as e:
            logger.warning(f"Balance check failed: {e}")

    # ========== Shutdown ==========

    async def shutdown(self):
        """
        Graceful exit with guaranteed position closing.
        Called with asyncio.wait_for(timeout=300) from arbitrage.py.
        """
        logger.info("=== SHUTDOWN STARTED ===")
        self._stop_flag = True

        # Step 1: Cancel all pending orders
        logger.info("Step 1: Canceling all orders...")
        try:
            await asyncio.wait_for(self.grvt_client.cancel_all_orders(), timeout=15)
        except Exception as e:
            logger.warning(f"GRVT cancel all failed: {e}")

        try:
            await asyncio.wait_for(self.lighter_client.cancel_all_orders(), timeout=15)
        except Exception as e:
            logger.warning(f"Lighter cancel all failed: {e}")

        await asyncio.sleep(1)  # Let cancellations settle

        # Step 2: Close positions with escalating slippage
        logger.info("Step 2: Closing positions...")
        await self._close_all_positions()

        # Step 3: Disconnect
        logger.info("Step 3: Disconnecting...")
        try:
            await self.grvt_client.disconnect()
        except Exception as e:
            logger.warning(f"GRVT disconnect error: {e}")

        try:
            await self.lighter_client.disconnect()
        except Exception as e:
            logger.warning(f"Lighter disconnect error: {e}")

        # Step 4: Final notifications
        runtime = (time.time() - self._start_time) / 3600
        await self.telegram.notify_stop(
            reason=self._stop_reason,
            runtime_hours=runtime,
            total_trades=self.order_manager.total_trades,
        )
        await self.telegram.close()
        self.data_logger.close()

        logger.info("=== SHUTDOWN COMPLETE ===")

    async def _close_all_positions(self):
        """Close positions on both exchanges with retry + escalating slippage."""
        min_size = self.config.order_quantity * Decimal("0.01")  # 1% of order qty

        for attempt, slippage in enumerate(SHUTDOWN_SLIPPAGE_LEVELS):
            logger.info(f"Close attempt {attempt + 1}/{len(SHUTDOWN_SLIPPAGE_LEVELS)}, slippage={slippage * 100}%")

            # Fetch real positions from API
            grvt_pos = await self._safe_get_position(self.grvt_client)
            lighter_pos = await self._safe_get_position(self.lighter_client)

            # Cross-check: take the larger absolute value (宁多平不漏平)
            grvt_close = self._pick_position(grvt_pos, self.positions.grvt_position, "GRVT")
            lighter_close = self._pick_position(lighter_pos, self.positions.lighter_position, "Lighter")

            if abs(grvt_close) < min_size and abs(lighter_close) < min_size:
                logger.info("Both positions cleared!")
                return

            # Close GRVT
            if abs(grvt_close) >= min_size:
                try:
                    success = await asyncio.wait_for(
                        self.grvt_client.close_position("", grvt_close, slippage),
                        timeout=20,
                    )
                    if success:
                        logger.info(f"GRVT position closed: {grvt_close}")
                except Exception as e:
                    logger.error(f"GRVT close failed: {e}")

            # Close Lighter
            if abs(lighter_close) >= min_size:
                try:
                    success = await asyncio.wait_for(
                        self.lighter_client.close_position("", lighter_close, slippage),
                        timeout=20,
                    )
                    if success:
                        logger.info(f"Lighter position closed: {lighter_close}")
                except Exception as e:
                    logger.error(f"Lighter close failed: {e}")

            await asyncio.sleep(5)  # Wait between attempts

        # Final check
        final_grvt = await self._safe_get_position(self.grvt_client)
        final_lighter = await self._safe_get_position(self.lighter_client)

        if abs(final_grvt) >= min_size or abs(final_lighter) >= min_size:
            msg = f"POSITIONS NOT CLOSED! GRVT={final_grvt} Lighter={final_lighter}"
            logger.error(msg)
            await self.telegram.notify_emergency(msg)

    async def _safe_get_position(self, client) -> Decimal:
        """Get position with retry, return 0 on failure."""
        for attempt in range(3):
            try:
                return await client.get_position()
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(1)
                    continue
                logger.error(f"Failed to get position after 3 attempts: {e}")
                return Decimal("0")
        return Decimal("0")

    @staticmethod
    def _pick_position(api_pos: Decimal, local_pos: Decimal, label: str) -> Decimal:
        """Take the position with larger absolute value (宁多平不漏平)."""
        if abs(api_pos) >= abs(local_pos):
            return api_pos
        logger.warning(f"{label}: using local pos {local_pos} (larger than API {api_pos})")
        return local_pos
