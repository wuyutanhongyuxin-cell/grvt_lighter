"""
Order lifecycle manager — the MOST CRITICAL module.

Phase 0: Pre-trade position refresh from API (both sides)
Phase 1: GRVT post-only order (up to 15 retries on taker rejection)
Phase 2: Wait for GRVT fill via WS event (asyncio.Event)
Phase 3: Lighter IOC order (size = confirmed GRVT fill, NEVER order_quantity)
Phase 4: Wait for Lighter fill via WS event
Phase 5: Record results, update positions

SAFETY RULES:
- If GRVT fill timeout → filled_size = 0, NO hedge (01-lighter's fallback=full_qty was fatal)
- Lighter size = confirmed GRVT filled_size ONLY
- If Lighter fill timeout → assume ZERO fill, send emergency alert
"""

import asyncio
import logging
import time
from decimal import Decimal
from typing import Optional

from exchanges.grvt_client import GrvtClient
from exchanges.lighter_client import LighterClient
from helpers.logger import DataLogger
from helpers.telegram import TelegramNotifier
from strategy.position_tracker import PositionTracker

logger = logging.getLogger("arbitrage.orders")

# Constants
POST_ONLY_MAX_RETRIES = 15
LIGHTER_FILL_TIMEOUT = 30  # seconds
CANCEL_CHECK_GRACE_TIMEOUT = 1.0  # seconds
VALID_DIRECTIONS = {"long_grvt", "short_grvt"}


class OrderManager:
    def __init__(
        self,
        grvt_client: GrvtClient,
        lighter_client: LighterClient,
        positions: PositionTracker,
        data_logger: DataLogger,
        telegram: TelegramNotifier,
        order_quantity: Decimal,
        fill_timeout: int,
        dashboard=None,
    ):
        self.grvt_client = grvt_client
        self.lighter_client = lighter_client
        self.positions = positions
        self.data_logger = data_logger
        self.telegram = telegram
        self.order_quantity = order_quantity
        self.fill_timeout = fill_timeout
        self.dashboard = dashboard

        self._executing = False
        self._trading_halted = False
        self._halt_reason = ""
        self.total_trades = 0

    async def execute_arb(self, direction: str) -> Optional[dict]:
        """
        Execute full arbitrage cycle.
        direction: "long_grvt" or "short_grvt"

        Returns trade result dict on success, None on failure/skip.
        """
        if self._trading_halted:
            logger.warning(f"Trading halted, skipping signal (reason={self._halt_reason})")
            return None

        if self._executing:
            logger.warning("Already executing, skipping")
            return None
        self._executing = True

        try:
            if direction not in VALID_DIRECTIONS:
                logger.error(f"Invalid direction: {direction}")
                return None

            # Determine sides
            if direction == "long_grvt":
                grvt_side = "buy"
                lighter_side = "sell"
            else:
                grvt_side = "sell"
                lighter_side = "buy"

            # ============ Phase 0: Pre-trade position refresh ============
            logger.info(f"=== PHASE 0: Pre-trade check ({direction}) ===")
            await self._refresh_positions()
            pre_pos_grvt = self.positions.grvt_position  # snapshot before order

            if not self.positions.check_pre_trade():
                logger.warning("Pre-trade position check failed, skipping")
                return None

            # Check direction capacity
            if direction == "long_grvt" and not self.positions.can_long_grvt():
                logger.info("Max long position reached, skipping")
                return None
            if direction == "short_grvt" and not self.positions.can_short_grvt():
                logger.info("Max short position reached, skipping")
                return None

            # ============ Phase 1: GRVT post-only order ============
            logger.info(f"=== PHASE 1: GRVT post-only {grvt_side} ===")
            grvt_fill_data = None

            for retry in range(POST_ONLY_MAX_RETRIES):
                # Get fresh BBO each attempt
                grvt_bid, grvt_ask = self.grvt_client.get_bbo()
                if grvt_bid is None or grvt_ask is None:
                    logger.warning("GRVT BBO not available, aborting")
                    return None

                # Price: buy at ask-tick, sell at bid+tick (ensure maker)
                tick = self.grvt_client.tick_size
                if grvt_side == "buy":
                    price = grvt_ask - tick
                else:
                    price = grvt_bid + tick

                try:
                    client_order_id = await self.grvt_client.place_post_only_order(
                        side=grvt_side, price=price, size=self.order_quantity,
                    )
                except Exception as e:
                    logger.error(f"GRVT place order failed: {e}")
                    return None

                # ============ Phase 2: Wait for GRVT fill via WS ============
                logger.info(f"=== PHASE 2: Waiting for GRVT fill (timeout={self.fill_timeout}s) ===")
                grvt_fill_data = await self.grvt_client.wait_for_fill(
                    client_order_id, timeout=self.fill_timeout, keep_pending_on_timeout=True,
                )

                if grvt_fill_data is None:
                    # Timeout => uncertain state. Run cancel-check + short reconcile window.
                    logger.info(f"GRVT fill timeout, entering cancel-check cid={client_order_id}")
                    cancel_ok = await self.grvt_client.cancel_order(client_order_id)
                    if not cancel_ok:
                        logger.warning(f"GRVT cancel failed/uncertain: cid={client_order_id}")
                    await asyncio.sleep(0.5)  # Let cancel process

                    # Check if partial fill came through before/during cancel
                    grvt_fill_data = self.grvt_client.get_last_fill(client_order_id)
                    if grvt_fill_data is None:
                        # One extra grace window for late WS fill after cancel.
                        grvt_fill_data = await self.grvt_client.wait_for_fill(
                            client_order_id, timeout=CANCEL_CHECK_GRACE_TIMEOUT,
                        )
                    else:
                        self.grvt_client.clear_pending_fill(client_order_id)
                    if grvt_fill_data and grvt_fill_data["filled_size"] > 0:
                        logger.info(f"Partial fill detected after cancel: {grvt_fill_data['filled_size']}")
                        break

                    # WS didn't report fill → REST position snapshot fallback
                    filled_qty = await self._get_filled_qty_from_snapshot(
                        pre_pos_grvt, grvt_side
                    )
                    if filled_qty > 0:
                        grvt_fill_data = {
                            "filled_size": filled_qty,
                            "price": price,  # approximate with order price
                            "status": "FILLED",
                        }
                        logger.info(f"Fill confirmed via position snapshot: {filled_qty}")
                        break

                    # No fill at all — retry with fresh BBO
                    logger.info(f"No fill, retry {retry + 1}/{POST_ONLY_MAX_RETRIES}")
                    continue

                if grvt_fill_data["status"] in ("REJECTED", "CANCELLED", "CANCELED"):
                    filled = grvt_fill_data.get("filled_size", Decimal("0"))
                    if filled > 0:
                        logger.info(f"GRVT order {grvt_fill_data['status']} with partial: {filled}")
                        break
                    logger.info(f"GRVT post-only rejected (not maker), retry {retry + 1}/{POST_ONLY_MAX_RETRIES}")
                    await asyncio.sleep(0.01)
                    continue

                # FILLED
                break

            # Validate fill result
            if not grvt_fill_data or grvt_fill_data.get("filled_size", Decimal("0")) <= 0:
                logger.info("GRVT order not filled after all attempts")
                return None

            confirmed_fill_size = grvt_fill_data["filled_size"]
            grvt_fill_price = grvt_fill_data.get("price", Decimal("0"))
            logger.info(f"GRVT confirmed: {grvt_side} {confirmed_fill_size} @ {grvt_fill_price}")

            # ============ Phase 3: Lighter IOC order ============
            # CRITICAL: size = confirmed_fill_size, NOT order_quantity
            logger.info(f"=== PHASE 3: Lighter IOC {lighter_side} {confirmed_fill_size} ===")
            try:
                lighter_client_idx = await self.lighter_client.place_ioc_order(
                    side=lighter_side, size=confirmed_fill_size,
                )
            except Exception as e:
                # CRITICAL: GRVT filled but Lighter failed!
                logger.error(
                    f"LIGHTER HEDGE FAILED! GRVT {grvt_side} {confirmed_fill_size}@{grvt_fill_price} "
                    f"filled, Lighter {lighter_side} error: {e}"
                )
                # Update only GRVT position — creates known imbalance
                self.positions.update_grvt(grvt_side, confirmed_fill_size)
                self._trading_halted = True
                self._halt_reason = "HEDGE_SUBMIT_FAILED"
                await self.telegram.notify_emergency(
                    f"HEDGE FAILURE: GRVT {grvt_side} {confirmed_fill_size} filled, "
                    f"Lighter {lighter_side} failed: {e}"
                )
                return None

            # ============ Phase 4: Wait for Lighter fill via WS ============
            logger.info(f"=== PHASE 4: Waiting for Lighter fill (timeout={LIGHTER_FILL_TIMEOUT}s) ===")
            lighter_fill_data = await self.lighter_client.wait_for_fill(
                lighter_client_idx, timeout=LIGHTER_FILL_TIMEOUT,
            )

            if lighter_fill_data is None:
                # CRITICAL: Lighter fill unknown — assume ZERO fill (safe default)
                logger.error(
                    f"LIGHTER FILL TIMEOUT! GRVT {grvt_side} {confirmed_fill_size}@{grvt_fill_price} "
                    f"confirmed, Lighter {lighter_side} status UNKNOWN"
                )
                self.positions.update_grvt(grvt_side, confirmed_fill_size)
                # Don't update lighter position (we don't know if it filled)
                self._trading_halted = True
                self._halt_reason = "HEDGE_FILL_UNKNOWN"
                await self.telegram.notify_emergency(
                    f"LIGHTER FILL TIMEOUT: GRVT {grvt_side} {confirmed_fill_size} confirmed, "
                    f"Lighter {lighter_side} fill unknown after {LIGHTER_FILL_TIMEOUT}s"
                )
                return None

            lighter_filled_size = lighter_fill_data.get("filled_size", Decimal("0"))
            lighter_fill_price = lighter_fill_data.get("avg_price", Decimal("0"))

            # ============ Phase 5: Record results ============
            logger.info(f"=== PHASE 5: Recording trade ===")
            self.positions.update_grvt(grvt_side, confirmed_fill_size)
            self.positions.update_lighter(lighter_side, lighter_filled_size)

            if lighter_filled_size < confirmed_fill_size:
                logger.warning(
                    f"Lighter partial fill: {lighter_filled_size}/{confirmed_fill_size} "
                    f"— imbalance of {confirmed_fill_size - lighter_filled_size}"
                )

            # Calculate captured spread
            if direction == "long_grvt":
                # Bought on GRVT, sold on Lighter → profit when lighter_price > grvt_price
                spread = lighter_fill_price - grvt_fill_price
            else:
                # Sold on GRVT, bought on Lighter → profit when grvt_price > lighter_price
                spread = grvt_fill_price - lighter_fill_price

            trade_result = {
                "direction": direction,
                "grvt_side": grvt_side,
                "grvt_price": grvt_fill_price,
                "grvt_size": confirmed_fill_size,
                "lighter_side": lighter_side,
                "lighter_price": lighter_fill_price,
                "lighter_size": lighter_filled_size,
                "spread": spread,
                "grvt_position": self.positions.grvt_position,
                "lighter_position": self.positions.lighter_position,
            }

            self.total_trades += 1
            self.data_logger.log_trade(**trade_result)

            profit_est = spread * confirmed_fill_size
            logger.info(
                f"TRADE COMPLETE: {direction} spread=${spread:.4f} "
                f"est_profit=${profit_est:.4f} "
                f"pos: GRVT={self.positions.grvt_position} Lighter={self.positions.lighter_position}"
            )

            # Dashboard update
            if self.dashboard:
                self.dashboard.add_trade(
                    direction=direction,
                    grvt_price=grvt_fill_price,
                    lighter_price=lighter_fill_price,
                    size=confirmed_fill_size,
                    spread=spread,
                    profit_est=profit_est,
                )
                self.dashboard.add_event("TRADE",
                    f"{direction} {confirmed_fill_size} spread=${spread:.4f} pnl=${profit_est:.4f}")

            # Notify
            await self.telegram.notify_trade(
                direction=direction,
                grvt_side=grvt_side, grvt_price=grvt_fill_price, grvt_size=confirmed_fill_size,
                lighter_side=lighter_side, lighter_price=lighter_fill_price, lighter_size=lighter_filled_size,
                spread_captured=spread,
                grvt_position=self.positions.grvt_position,
                lighter_position=self.positions.lighter_position,
            )

            return trade_result

        except Exception as e:
            logger.error(f"Unexpected error in execute_arb: {e}", exc_info=True)
            return None
        finally:
            self._executing = False

    async def _refresh_positions(self):
        """Refresh positions from API before each trade (QuantGuy pattern)."""
        try:
            grvt_pos = await self.grvt_client.get_position()
            self.positions.grvt_position = grvt_pos
        except Exception as e:
            logger.warning(f"Failed to refresh GRVT position: {e}, using local tracker")

        try:
            lighter_pos = await self.lighter_client.get_position()
            self.positions.lighter_position = lighter_pos
        except Exception as e:
            logger.warning(f"Failed to refresh Lighter position: {e}, using local tracker")

        logger.info(
            f"Positions refreshed: GRVT={self.positions.grvt_position} "
            f"Lighter={self.positions.lighter_position} "
            f"net={self.positions.get_net_position()}"
        )

    async def _get_filled_qty_from_snapshot(
        self, pre_pos: Decimal, side: str
    ) -> Decimal:
        """Detect fill via position snapshot diff (01-lighter pattern).

        Compares pre-order position with current API position.
        Returns confirmed fill quantity, capped at order_quantity.
        """
        for attempt in range(3):
            try:
                post_pos = await self.grvt_client.get_position()
                diff = post_pos - pre_pos
                if side == "buy":
                    filled = max(diff, Decimal("0"))
                else:
                    filled = max(-diff, Decimal("0"))
                filled = min(filled, self.order_quantity)  # physical cap
                if filled > 0:
                    logger.info(
                        f"Snapshot fill check: pre={pre_pos} post={post_pos} "
                        f"diff={diff} filled={filled}"
                    )
                    return filled
            except Exception as e:
                logger.warning(f"Snapshot fill check attempt {attempt + 1} failed: {e}")
            await asyncio.sleep(0.5)
        return Decimal("0")

    @property
    def is_trading_halted(self) -> bool:
        return self._trading_halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason
