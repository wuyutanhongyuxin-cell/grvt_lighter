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
POST_ONLY_MAX_RETRIES = 8
LIGHTER_FILL_TIMEOUT = 2  # seconds (IOC fills instantly, just wait for WS before REST fallback)
REST_POLL_INTERVAL = 0.5  # seconds — concurrent REST poll interval
CANCEL_CHECK_GRACE_TIMEOUT = 0.5  # seconds
CANCEL_RPC_TIMEOUT = 2.0  # seconds
POST_CANCEL_SETTLE_DELAY = 0.2  # seconds
GRVT_IOC_SLIPPAGE_PCT = Decimal("0.002")  # 0.2% slippage for GRVT IOC
MARKET_MARKET_FILL_TIMEOUT = 2  # seconds — IOC should fill instantly
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
                grvt_fill_data = await self._wait_grvt_fill_concurrent(
                    client_order_id, pre_pos_grvt, grvt_side, price, self.fill_timeout,
                )

                if grvt_fill_data is None:
                    # Timeout => uncertain state. Cancel ALL orders (not just this one)
                    # to kill any ghost orders from previous retries too.
                    logger.info(f"GRVT fill timeout, canceling all orders (cid={client_order_id})")
                    try:
                        await asyncio.wait_for(
                            self.grvt_client.cancel_all_orders(),
                            timeout=CANCEL_RPC_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        logger.warning("GRVT cancel_all rpc timeout")
                    except Exception as e:
                        logger.warning(f"GRVT cancel_all failed: {e}")
                    await asyncio.sleep(POST_CANCEL_SETTLE_DELAY)  # Let cancel process

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
                        filled = min(grvt_fill_data["filled_size"], self.order_quantity)
                        grvt_fill_data["filled_size"] = filled
                        logger.info(f"Partial fill detected after cancel: {filled}")
                        break

                    # WS didn't report fill → REST position snapshot fallback
                    filled_qty = await self._get_filled_qty_from_snapshot(
                        pre_pos_grvt, grvt_side
                    )
                    if filled_qty > 0:
                        # Cap at order_quantity: only hedge what THIS trade intended.
                        # Ghost orders from earlier retries are absorbed by pre_pos refresh.
                        filled_qty = min(filled_qty, self.order_quantity)
                        grvt_fill_data = {
                            "filled_size": filled_qty,
                            "price": price,  # approximate with order price
                            "status": "FILLED",
                        }
                        self.grvt_client.clear_pending_fill(client_order_id)
                        logger.info(f"Fill confirmed via position snapshot: {filled_qty}")
                        break

                    # No fill at all — refresh pre_pos to absorb any ghost fills
                    # from earlier retries before next iteration.
                    try:
                        pre_pos_grvt = await self.grvt_client.get_position()
                        logger.debug(f"Pre-pos refreshed for retry: {pre_pos_grvt}")
                    except Exception:
                        pass  # keep old pre_pos on failure
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

            # Record Lighter pre-position for Phase 4 snapshot fallback
            pre_pos_lighter = self.positions.lighter_position

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
            lighter_fill_data = await self._wait_lighter_fill_concurrent(
                lighter_client_idx, pre_pos_lighter, lighter_side, confirmed_fill_size,
            )

            if lighter_fill_data is None:
                # WS didn't report fill → REST position snapshot fallback
                logger.warning(
                    f"Lighter WS fill timeout, checking REST snapshot "
                    f"(pre_pos={pre_pos_lighter})"
                )
                lighter_filled_qty = await self._get_lighter_filled_qty_from_snapshot(
                    pre_pos_lighter, lighter_side, confirmed_fill_size,
                )
                if lighter_filled_qty > 0:
                    # Lighter DID fill, construct fill_data from snapshot
                    lighter_bid, lighter_ask = self.lighter_client.get_bbo_unfiltered()
                    est_price = lighter_bid if lighter_side == "sell" else lighter_ask
                    lighter_fill_data = {
                        "filled_size": lighter_filled_qty,
                        "avg_price": est_price or Decimal("0"),
                        "status": "FILLED",
                    }
                    logger.info(f"Lighter fill confirmed via snapshot: {lighter_filled_qty}")
                else:
                    # Genuinely unknown
                    logger.error(
                        f"LIGHTER FILL TIMEOUT! GRVT {grvt_side} {confirmed_fill_size}@{grvt_fill_price} "
                        f"confirmed, Lighter {lighter_side} status UNKNOWN"
                    )
                    self.positions.update_grvt(grvt_side, confirmed_fill_size)
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
                imbalance = confirmed_fill_size - lighter_filled_size
                msg = (
                    f"Lighter partial fill: {lighter_filled_size}/{confirmed_fill_size} "
                    f"— imbalance of {imbalance}"
                )
                logger.error(msg)
                self._trading_halted = True
                self._halt_reason = "LIGHTER_PARTIAL_FILL"
                await self.telegram.notify_emergency(
                    f"LIGHTER PARTIAL FILL: {msg}. Trading halted."
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
            # If GRVT already filled, we have unhedged exposure → halt
            if grvt_fill_data and grvt_fill_data.get("filled_size", Decimal("0")) > 0:
                self._trading_halted = True
                self._halt_reason = "POST_FILL_EXCEPTION"
                await self.telegram.notify_emergency(
                    f"Exception after GRVT fill! "
                    f"filled={grvt_fill_data['filled_size']} err={e}"
                )
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

    async def _wait_grvt_fill_concurrent(
        self,
        client_order_id: str,
        pre_pos: Decimal,
        side: str,
        price: Decimal,
        timeout: float,
    ) -> Optional[dict]:
        """WS + REST concurrent race for GRVT fill detection.

        Loops until deadline: short WS wait (REST_POLL_INTERVAL) → REST position poll.
        Returns fill_data on success, None on timeout (existing fallback code handles it).
        """
        deadline = time.time() + timeout

        while time.time() < deadline:
            remaining = max(deadline - time.time(), 0.01)
            ws_wait = min(REST_POLL_INTERVAL, remaining)

            # Short WS wait
            fill_data = await self.grvt_client.wait_for_fill(
                client_order_id, timeout=ws_wait, keep_pending_on_timeout=True,
            )
            if fill_data is not None:
                logger.info(f"GRVT fill via WS (concurrent): cid={client_order_id}")
                return fill_data

            # REST position poll
            try:
                post_pos = await self.grvt_client.get_position()
                diff = post_pos - pre_pos
                if side == "buy":
                    filled = max(diff, Decimal("0"))
                else:
                    filled = max(-diff, Decimal("0"))

                if filled > 0:
                    filled = min(filled, self.order_quantity)  # cap at order_quantity
                    # Fill detected via REST — cancel all remaining orders (partial fill
                    # may leave the rest of the order alive on the book).
                    logger.info(
                        f"GRVT fill via REST poll: pre={pre_pos} post={post_pos} "
                        f"filled={filled}, canceling remaining orders"
                    )
                    try:
                        await asyncio.wait_for(
                            self.grvt_client.cancel_all_orders(),
                            timeout=CANCEL_RPC_TIMEOUT,
                        )
                    except (asyncio.TimeoutError, Exception) as e:
                        logger.warning(f"GRVT cancel_all after REST fill: {e}")

                    self.grvt_client.clear_pending_fill(client_order_id)
                    return {
                        "filled_size": filled,
                        "price": price,
                        "status": "FILLED",
                    }
            except Exception as e:
                logger.warning(f"GRVT REST poll failed: {e}")

        # Timeout — return None so existing fallback code (cancel → grace → snapshot) runs.
        return None

    async def _wait_lighter_fill_concurrent(
        self,
        client_order_index: int,
        pre_pos: Decimal,
        side: str,
        max_qty: Decimal,
    ) -> Optional[dict]:
        """WS + REST concurrent race for Lighter fill detection.

        Loops until LIGHTER_FILL_TIMEOUT: short WS wait → REST position poll.
        IOC orders are terminal — no cancel needed on fill.
        Returns fill_data on success, None on timeout (existing fallback code handles it).
        """
        deadline = time.time() + LIGHTER_FILL_TIMEOUT

        while time.time() < deadline:
            remaining = max(deadline - time.time(), 0.01)
            ws_wait = min(REST_POLL_INTERVAL, remaining)

            # Short WS wait
            fill_data = await self.lighter_client.wait_for_fill(
                client_order_index, timeout=ws_wait, keep_pending_on_timeout=True,
            )
            if fill_data is not None:
                logger.info(f"Lighter fill via WS (concurrent): idx={client_order_index}")
                self.lighter_client._clear_pending_fill(client_order_index)
                return fill_data

            # REST position poll
            try:
                post_pos = await self.lighter_client.get_position()
                diff = post_pos - pre_pos
                if side == "buy":
                    filled = max(diff, Decimal("0"))
                else:
                    filled = max(-diff, Decimal("0"))

                if filled > 0:
                    filled = min(filled, max_qty)  # cap at order_quantity
                    lighter_bid, lighter_ask = self.lighter_client.get_bbo_unfiltered()
                    est_price = lighter_bid if side == "sell" else lighter_ask
                    logger.info(
                        f"Lighter fill via REST poll: pre={pre_pos} post={post_pos} filled={filled}"
                    )
                    # IOC is terminal — no cancel needed
                    self.lighter_client._clear_pending_fill(client_order_index)
                    return {
                        "filled_size": filled,
                        "avg_price": est_price or Decimal("0"),
                        "status": "FILLED",
                    }
            except Exception as e:
                logger.warning(f"Lighter REST poll failed: {e}")

        # Timeout — cleanup pending fill and return None for existing fallback
        self.lighter_client._clear_pending_fill(client_order_index)
        return None

    async def _get_lighter_filled_qty_from_snapshot(
        self, pre_pos: Decimal, side: str, max_qty: Decimal,
    ) -> Decimal:
        """Detect Lighter fill via position snapshot diff."""
        for attempt in range(3):
            try:
                post_pos = await self.lighter_client.get_position()
                diff = post_pos - pre_pos
                if side == "buy":
                    filled = max(diff, Decimal("0"))
                else:
                    filled = max(-diff, Decimal("0"))
                filled = min(filled, max_qty)  # physical cap
                logger.info(
                    f"Lighter snapshot attempt {attempt + 1}/3: "
                    f"pre={pre_pos} post={post_pos} diff={diff} filled={filled}"
                )
                if filled > 0:
                    logger.info(
                        f"Lighter snapshot fill check: pre={pre_pos} post={post_pos} "
                        f"diff={diff} filled={filled}"
                    )
                    return filled
            except Exception as e:
                logger.warning(f"Lighter snapshot fill check attempt {attempt + 1} failed: {e}")
            await asyncio.sleep(0.5)
        return Decimal("0")

    async def _get_filled_qty_from_snapshot(
        self, pre_pos: Decimal, side: str
    ) -> Decimal:
        """Detect fill via position snapshot diff (01-lighter pattern).

        Compares pre-order position with current API position.
        Returns confirmed fill quantity, capped at the physical maximum
        (order_quantity × POST_ONLY_MAX_RETRIES) to guard against API glitches.
        """
        max_possible = self.order_quantity * POST_ONLY_MAX_RETRIES
        for attempt in range(3):
            try:
                post_pos = await self.grvt_client.get_position()
                diff = post_pos - pre_pos
                if side == "buy":
                    filled = max(diff, Decimal("0"))
                else:
                    filled = max(-diff, Decimal("0"))
                filled = min(filled, max_possible)  # physical cap
                if filled > 0:
                    logger.info(
                        f"Snapshot fill check: pre={pre_pos} post={post_pos} "
                        f"diff={diff} filled={filled}"
                    )
                    return filled
            except Exception as e:
                logger.warning(f"Snapshot fill check attempt {attempt + 1} failed: {e}")
            await asyncio.sleep(0.35)
        return Decimal("0")

    # ============ Market-Market Mode ============

    async def execute_arb_market_market(self, direction: str) -> Optional[dict]:
        """
        Execute arbitrage with simultaneous IOC orders on both exchanges.
        direction: "long_grvt" or "short_grvt"

        Both legs fire concurrently via asyncio.gather, eliminating adverse selection.
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
            logger.info(f"=== MM PHASE 0: Pre-trade check ({direction}) ===")
            await self._refresh_positions()

            if not self.positions.check_pre_trade():
                logger.warning("Pre-trade position check failed, skipping")
                return None

            if direction == "long_grvt" and not self.positions.can_long_grvt():
                logger.info("Max long position reached, skipping")
                return None
            if direction == "short_grvt" and not self.positions.can_short_grvt():
                logger.info("Max short position reached, skipping")
                return None

            # Snapshot pre-positions from API (already refreshed above)
            pre_pos_grvt = self.positions.grvt_position
            pre_pos_lighter = self.positions.lighter_position

            # ============ Phase 1: Calculate IOC prices ============
            logger.info(f"=== MM PHASE 1: Calculate IOC prices ===")
            grvt_bid, grvt_ask = self.grvt_client.get_bbo()
            lighter_bid, lighter_ask = self.lighter_client.get_bbo_unfiltered()

            if any(v is None for v in (grvt_bid, grvt_ask, lighter_bid, lighter_ask)):
                logger.warning("BBO not available on one or both exchanges, aborting")
                return None

            # GRVT IOC price with slippage (tick-aligned inside place_ioc_order)
            if grvt_side == "buy":
                grvt_price = grvt_ask * (Decimal("1") + GRVT_IOC_SLIPPAGE_PCT)
            else:
                grvt_price = grvt_bid * (Decimal("1") - GRVT_IOC_SLIPPAGE_PCT)

            # Lighter IOC price computed inside place_ioc_order (uses its own slippage)

            # ============ Phase 2: Simultaneous IOC orders ============
            logger.info(
                f"=== MM PHASE 2: Simultaneous IOC {grvt_side}/{lighter_side} "
                f"size={self.order_quantity} ==="
            )

            results = await asyncio.gather(
                self._execute_grvt_ioc_leg(grvt_side, grvt_price, self.order_quantity, pre_pos_grvt),
                self._execute_lighter_ioc_leg(lighter_side, self.order_quantity, pre_pos_lighter),
                return_exceptions=True,
            )

            # Unpack — gather with return_exceptions=True returns exceptions as values
            grvt_result = results[0] if not isinstance(results[0], BaseException) else None
            lighter_result = results[1] if not isinstance(results[1], BaseException) else None

            if isinstance(results[0], BaseException):
                logger.error(f"GRVT IOC leg exception: {results[0]}")
            if isinstance(results[1], BaseException):
                logger.error(f"Lighter IOC leg exception: {results[1]}")

            grvt_filled = grvt_result is not None and grvt_result["filled_size"] > 0
            lighter_filled = lighter_result is not None and lighter_result["filled_size"] > 0

            # ============ Phase 3: Handle results ============
            logger.info(f"=== MM PHASE 3: Handle results (GRVT={grvt_filled}, Lighter={lighter_filled}) ===")

            if not grvt_filled and not lighter_filled:
                # Both sides failed — no exposure, safe to return
                logger.info("MM: Both sides unfilled, no action needed")
                return None

            if grvt_filled and not lighter_filled:
                # GRVT filled but Lighter didn't → unhedged exposure → halt
                grvt_filled_size = grvt_result["filled_size"]
                self.positions.update_grvt(grvt_side, grvt_filled_size)
                self._trading_halted = True
                self._halt_reason = "MM_SINGLE_LEG_FILL"
                await self.telegram.notify_emergency(
                    f"MARKET_MARKET single leg: GRVT {grvt_side} {grvt_filled_size} filled, "
                    f"Lighter {lighter_side} FAILED"
                )
                logger.error(
                    f"MM SINGLE LEG: GRVT {grvt_side} {grvt_filled_size} filled, "
                    f"Lighter {lighter_side} failed → HALT"
                )
                return None

            if lighter_filled and not grvt_filled:
                # Lighter filled but GRVT didn't → unhedged exposure → halt
                lighter_filled_size = lighter_result["filled_size"]
                self.positions.update_lighter(lighter_side, lighter_filled_size)
                self._trading_halted = True
                self._halt_reason = "MM_SINGLE_LEG_FILL"
                await self.telegram.notify_emergency(
                    f"MARKET_MARKET single leg: Lighter {lighter_side} {lighter_filled_size} filled, "
                    f"GRVT {grvt_side} FAILED"
                )
                logger.error(
                    f"MM SINGLE LEG: Lighter {lighter_side} {lighter_filled_size} filled, "
                    f"GRVT {grvt_side} failed → HALT"
                )
                return None

            # ============ Phase 4: Both filled — record trade ============
            grvt_filled_size = grvt_result["filled_size"]
            grvt_fill_price = grvt_result["price"]
            lighter_filled_size = lighter_result["filled_size"]
            lighter_fill_price = lighter_result["price"]

            logger.info(f"=== MM PHASE 4: Recording trade ===")
            self.positions.update_grvt(grvt_side, grvt_filled_size)
            self.positions.update_lighter(lighter_side, lighter_filled_size)

            # Check size mismatch
            if grvt_filled_size != lighter_filled_size:
                imbalance = abs(grvt_filled_size - lighter_filled_size)
                msg = (
                    f"MM size mismatch: GRVT {grvt_side} {grvt_filled_size}, "
                    f"Lighter {lighter_side} {lighter_filled_size} — imbalance {imbalance}"
                )
                logger.error(msg)
                self._trading_halted = True
                self._halt_reason = "MM_SIZE_MISMATCH"
                await self.telegram.notify_emergency(f"{msg}. Trading halted.")

            # Calculate captured spread
            if direction == "long_grvt":
                spread = lighter_fill_price - grvt_fill_price
            else:
                spread = grvt_fill_price - lighter_fill_price

            trade_result = {
                "direction": direction,
                "grvt_side": grvt_side,
                "grvt_price": grvt_fill_price,
                "grvt_size": grvt_filled_size,
                "lighter_side": lighter_side,
                "lighter_price": lighter_fill_price,
                "lighter_size": lighter_filled_size,
                "spread": spread,
                "grvt_position": self.positions.grvt_position,
                "lighter_position": self.positions.lighter_position,
            }

            self.total_trades += 1
            self.data_logger.log_trade(**trade_result)

            profit_est = spread * min(grvt_filled_size, lighter_filled_size)
            logger.info(
                f"MM TRADE COMPLETE: {direction} spread=${spread:.4f} "
                f"est_profit=${profit_est:.4f} "
                f"pos: GRVT={self.positions.grvt_position} Lighter={self.positions.lighter_position}"
            )

            # Dashboard update
            if self.dashboard:
                self.dashboard.add_trade(
                    direction=direction,
                    grvt_price=grvt_fill_price,
                    lighter_price=lighter_fill_price,
                    size=min(grvt_filled_size, lighter_filled_size),
                    spread=spread,
                    profit_est=profit_est,
                )
                self.dashboard.add_event("MM_TRADE",
                    f"{direction} {self.order_quantity} spread=${spread:.4f} pnl=${profit_est:.4f}")

            # Notify
            await self.telegram.notify_trade(
                direction=direction,
                grvt_side=grvt_side, grvt_price=grvt_fill_price, grvt_size=grvt_filled_size,
                lighter_side=lighter_side, lighter_price=lighter_fill_price, lighter_size=lighter_filled_size,
                spread_captured=spread,
                grvt_position=self.positions.grvt_position,
                lighter_position=self.positions.lighter_position,
            )

            return trade_result

        except Exception as e:
            logger.error(f"Unexpected error in execute_arb_market_market: {e}", exc_info=True)
            return None
        finally:
            self._executing = False

    async def _execute_grvt_ioc_leg(
        self, side: str, price: Decimal, size: Decimal, pre_pos: Decimal,
    ) -> Optional[dict]:
        """Execute single GRVT IOC leg + confirm via WS/REST. Never raises."""
        try:
            client_order_id = await self.grvt_client.place_ioc_order(
                side=side, price=price, size=size,
            )

            # Wait for fill via WS
            fill_data = await self.grvt_client.wait_for_fill(
                client_order_id, timeout=MARKET_MARKET_FILL_TIMEOUT,
            )

            if fill_data and fill_data.get("filled_size", Decimal("0")) > 0:
                filled = min(fill_data["filled_size"], self.order_quantity)
                fill_price = fill_data.get("price", Decimal("0"))
                logger.info(f"GRVT IOC filled via WS: {filled} @ {fill_price}")
                return {"filled_size": filled, "price": fill_price}

            # WS didn't report fill → REST snapshot fallback
            logger.info("GRVT IOC WS timeout, checking REST snapshot...")
            filled_qty = await self._get_filled_qty_from_snapshot(pre_pos, side)
            if filled_qty > 0:
                filled_qty = min(filled_qty, self.order_quantity)
                logger.info(f"GRVT IOC fill confirmed via snapshot: {filled_qty}")
                return {"filled_size": filled_qty, "price": price}

            logger.info("GRVT IOC: no fill detected")
            return None

        except Exception as e:
            logger.error(f"GRVT IOC leg error: {e}", exc_info=True)
            # Check REST snapshot in case order was accepted despite error
            try:
                filled_qty = await self._get_filled_qty_from_snapshot(pre_pos, side)
                if filled_qty > 0:
                    filled_qty = min(filled_qty, self.order_quantity)
                    logger.warning(f"GRVT IOC fill detected via snapshot after error: {filled_qty}")
                    return {"filled_size": filled_qty, "price": price}
            except Exception:
                pass
            return None

    async def _execute_lighter_ioc_leg(
        self, side: str, size: Decimal, pre_pos: Decimal,
    ) -> Optional[dict]:
        """Execute single Lighter IOC leg + confirm via WS/REST. Never raises."""
        try:
            client_order_index = await self.lighter_client.place_ioc_order(
                side=side, size=size,
            )

            # Wait for fill via WS
            fill_data = await self.lighter_client.wait_for_fill(
                client_order_index, timeout=MARKET_MARKET_FILL_TIMEOUT,
            )

            if fill_data and fill_data.get("filled_size", Decimal("0")) > 0:
                filled = min(fill_data["filled_size"], self.order_quantity)
                fill_price = fill_data.get("avg_price", Decimal("0"))
                logger.info(f"Lighter IOC filled via WS: {filled} @ {fill_price}")
                self.lighter_client._clear_pending_fill(client_order_index)
                return {"filled_size": filled, "price": fill_price}

            # WS didn't report fill → REST snapshot fallback
            logger.info("Lighter IOC WS timeout, checking REST snapshot...")
            filled_qty = await self._get_lighter_filled_qty_from_snapshot(
                pre_pos, side, size,
            )
            if filled_qty > 0:
                filled_qty = min(filled_qty, self.order_quantity)
                lighter_bid, lighter_ask = self.lighter_client.get_bbo_unfiltered()
                est_price = lighter_bid if side == "sell" else lighter_ask
                logger.info(f"Lighter IOC fill confirmed via snapshot: {filled_qty}")
                self.lighter_client._clear_pending_fill(client_order_index)
                return {"filled_size": filled_qty, "price": est_price or Decimal("0")}

            logger.info("Lighter IOC: no fill detected")
            self.lighter_client._clear_pending_fill(client_order_index)
            return None

        except Exception as e:
            logger.error(f"Lighter IOC leg error: {e}", exc_info=True)
            # Check REST snapshot in case order was accepted despite error
            try:
                filled_qty = await self._get_lighter_filled_qty_from_snapshot(
                    pre_pos, side, size,
                )
                if filled_qty > 0:
                    filled_qty = min(filled_qty, self.order_quantity)
                    lighter_bid, lighter_ask = self.lighter_client.get_bbo_unfiltered()
                    est_price = lighter_bid if side == "sell" else lighter_ask
                    logger.warning(f"Lighter IOC fill detected via snapshot after error: {filled_qty}")
                    return {"filled_size": filled_qty, "price": est_price or Decimal("0")}
            except Exception:
                pass
            return None

    @property
    def is_trading_halted(self) -> bool:
        return self._trading_halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason
