"""
GRVT exchange client — Maker side (post-only orders).

Uses grvt-pysdk GrvtCcxtWS for WebSocket streaming + order placement.
Fill detection via WS 'order' feed with asyncio.Event confirmation.
"""

import asyncio
import logging
import os
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Optional, Tuple

from .base import BaseExchangeClient

logger = logging.getLogger("arbitrage.grvt")

# Constants
MINI_TICKER_RATE = 100  # ms
ORDER_DURATION = 300  # seconds — post-only order TTL
WS_STALE_THRESHOLD = 30  # seconds
SEND_ERROR_GRACE_TIMEOUT = 1.5  # seconds

# Ticker mapping: our short names → GRVT symbol format
TICKER_MAP = {
    "BTC": "BTC_USDT_Perp",
    "ETH": "ETH_USDT_Perp",
}


class GrvtClient(BaseExchangeClient):
    def __init__(
        self,
        private_key: str,
        api_key: str,
        trading_account_id: str,
        env: str,
        ticker: str,
    ):
        self._private_key = private_key
        self._api_key = api_key
        self._trading_account_id = trading_account_id
        self._env = env
        self._ticker = ticker
        self._symbol = TICKER_MAP.get(ticker, f"{ticker}_USDT_Perp")

        # SDK client
        self._ws = None  # GrvtCcxtWS instance

        # BBO state (from mini ticker)
        self._best_bid: Optional[Decimal] = None
        self._best_ask: Optional[Decimal] = None
        self._last_ticker_time: float = 0

        # Fill tracking — asyncio.Event per client_order_id
        self._pending_fills: dict[str, asyncio.Event] = {}
        self._fill_results: dict[str, dict] = {}

        # Tick size (will be populated from market data)
        self._tick_size: Decimal = Decimal("0.1")  # Default, updated on connect
        self._ticker_format_logged: bool = False  # Log raw format once for debugging

    # ========== Connection ==========

    async def connect(self) -> None:
        # Set environment variables required by grvt-pysdk
        os.environ["GRVT_PRIVATE_KEY"] = self._private_key
        os.environ["GRVT_API_KEY"] = self._api_key
        os.environ["GRVT_TRADING_ACCOUNT_ID"] = self._trading_account_id
        os.environ["GRVT_ENV"] = self._env

        from pysdk.grvt_ccxt_ws import GrvtCcxtWS
        from pysdk.grvt_ccxt_env import GrvtEnv, GrvtWSEndpointType

        params = {
            "private_key": self._private_key,
            "api_key": self._api_key,
            "trading_account_id": self._trading_account_id,
        }

        env = GrvtEnv(self._env)
        loop = asyncio.get_event_loop()
        self._ws = GrvtCcxtWS(env, loop=loop, logger=logger, parameters=params)
        await self._ws.initialize()
        logger.info("GRVT WS initialized")

        # Subscribe to mini ticker for BBO
        await self._ws.subscribe(
            stream="mini.s",
            callback=self._on_mini_ticker,
            ws_end_point_type=GrvtWSEndpointType.MARKET_DATA_RPC_FULL,
            params={"instrument": self._symbol},
        )
        logger.info(f"Subscribed to GRVT mini ticker: {self._symbol}")

        # Subscribe to order feed for fill detection (filtered by instrument)
        await self._ws.subscribe(
            stream="order",
            callback=self._on_order_update,
            ws_end_point_type=GrvtWSEndpointType.TRADE_DATA_RPC_FULL,
            params={"instrument": self._symbol},
        )
        logger.info(f"Subscribed to GRVT order feed: {self._symbol}")

        # Fetch tick size from market info
        await self._fetch_tick_size()

        # Wait for first BBO
        for _ in range(100):  # 10s max
            if self._best_bid is not None and self._best_ask is not None:
                logger.info(f"GRVT BBO ready: bid={self._best_bid} ask={self._best_ask}")
                break
            await asyncio.sleep(0.1)
        else:
            logger.warning("GRVT BBO not available within 10s, continuing")

        # Verify trade data channel is connected (critical for order fills)
        await self._verify_trade_channels()

    async def _verify_trade_channels(self) -> None:
        """Check that trade data WS channels actually connected (cookie auth may silently fail)."""
        if not self._ws:
            return
        try:
            from pysdk.grvt_ccxt_env import GrvtWSEndpointType
            tdg_open = self._ws.is_connection_open(GrvtWSEndpointType.TRADE_DATA_RPC_FULL)
            cookie_ok = self._ws._cookie is not None
            if not cookie_ok:
                logger.error(
                    "GRVT cookie authentication FAILED — trade data channels NOT connected. "
                    "Order placement and fill detection will NOT work. "
                    "Check GRVT_API_KEY and GRVT_PRIVATE_KEY in .env"
                )
            elif not tdg_open:
                logger.error(
                    "GRVT trade data WS (tdg_rpc_full) not connected despite valid cookie. "
                    "Order feed subscription will not receive fill events."
                )
            else:
                logger.info("GRVT trade data channel verified: connected and authenticated")
        except Exception as e:
            logger.warning(f"Could not verify GRVT trade channels: {e}")

    async def disconnect(self) -> None:
        if self._ws:
            try:
                from pysdk.grvt_ccxt_env import GrvtWSEndpointType
                for ep in GrvtWSEndpointType:
                    try:
                        await self._ws._close_connection(ep)
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"GRVT WS close error: {e}")
        self._ws = None
        self._best_bid = None
        self._best_ask = None
        logger.info("GRVT disconnected")

    async def _fetch_tick_size(self):
        """Fetch tick size from GRVT market data."""
        try:
            if self._ws and hasattr(self._ws, 'markets') and self._ws.markets:
                market = self._ws.markets.get(self._symbol)
                if market:
                    parsed = self._parse_tick_size_from_market(market)
                    if parsed is not None and parsed > 0:
                        self._tick_size = parsed
                        logger.info(f"GRVT tick size: {self._tick_size}")
                        return
            # Fallback defaults
            if "BTC" in self._ticker:
                self._tick_size = Decimal("0.1")
            elif "ETH" in self._ticker:
                self._tick_size = Decimal("0.01")
            logger.info(f"GRVT tick size (fallback): {self._tick_size}")
        except Exception as e:
            logger.warning(f"Failed to fetch GRVT tick size: {e}")

    @staticmethod
    def _to_decimal(value) -> Optional[Decimal]:
        try:
            if value is None:
                return None
            return Decimal(str(value))
        except Exception:
            return None

    @classmethod
    def _parse_tick_size_from_market(cls, market: dict) -> Optional[Decimal]:
        """Parse tick size robustly across different market schemas."""
        if not isinstance(market, dict):
            return None

        # Prefer explicit tick/min-price style fields first.
        candidates = [
            market.get("tick_size"),
            market.get("price_tick"),
            market.get("limits", {}).get("price", {}).get("min"),
            market.get("info", {}).get("tick_size"),
            market.get("info", {}).get("price_tick"),
        ]
        for raw in candidates:
            dec = cls._to_decimal(raw)
            if dec is not None and dec > 0:
                return dec

        # Fallback: precision.price can be either decimal places (int)
        # or direct tick size (fractional).
        precision_price = market.get("precision", {}).get("price")
        dec = cls._to_decimal(precision_price)
        if dec is None or dec <= 0:
            return None
        if dec < 1:
            return dec
        if dec == dec.to_integral_value() and dec <= 12:
            return Decimal("1").scaleb(-int(dec))
        return None

    @staticmethod
    def _normalize_symbol(value: str) -> str:
        """Normalize symbol/instrument strings for robust matching."""
        return str(value or "").strip().replace("-", "_").upper()

    def _matches_position_symbol(self, pos: dict) -> bool:
        """Match position payload symbol across schema variants."""
        expected = self._normalize_symbol(self._symbol)
        candidates = (
            pos.get("instrument"),
            pos.get("symbol"),
            pos.get("market"),
            pos.get("ticker"),
        )
        return any(self._normalize_symbol(raw) == expected for raw in candidates if raw)

    @classmethod
    def _extract_signed_position(cls, pos: dict) -> Decimal:
        """Extract signed position size from mixed schemas."""
        signed_size: Optional[Decimal] = None
        for key in ("size", "contracts", "positionAmt", "position_amt", "qty", "quantity"):
            if key not in pos:
                continue
            dec = cls._to_decimal(pos.get(key))
            if dec is not None:
                signed_size = dec
                break

        if signed_size is None:
            return Decimal("0")

        side = str(pos.get("side", "")).strip().lower()
        notional = cls._to_decimal(pos.get("notional"))

        if side in ("short", "sell") and signed_size > 0:
            signed_size = -signed_size
        elif side in ("long", "buy") and signed_size < 0:
            logger.warning(
                f"GRVT position side conflicts with signed size: side={side} size={signed_size}"
            )
        elif side == "" and signed_size != 0 and notional is not None and notional != 0:
            # Some payloads use absolute size with signed notional.
            if signed_size * notional < 0:
                signed_size = -signed_size

        return signed_size

    # ========== WS Callbacks ==========

    async def _on_mini_ticker(self, data: dict):
        """Handle mini ticker updates for BBO."""
        try:
            # Log raw data format once for debugging
            if not self._ticker_format_logged:
                logger.info(f"GRVT mini ticker raw sample: {str(data)[:500]}")
                self._ticker_format_logged = True

            # SDK passes the entire message; BBO lives inside data["feed"]
            feed = data.get("feed", data) if isinstance(data, dict) else {}

            # Try various field names the SDK might use
            bid = feed.get("best_bid_price") or feed.get("best_bid") or feed.get("bid")
            ask = feed.get("best_ask_price") or feed.get("best_ask") or feed.get("ask")

            # Handle nested "result" wrapper (some SDK versions)
            if bid is None and "result" in feed:
                result = feed["result"]
                bid = result.get("best_bid_price") or result.get("best_bid") or result.get("bid")
                ask = result.get("best_ask_price") or result.get("best_ask") or result.get("ask")

            if bid is not None and ask is not None:
                self._best_bid = Decimal(str(bid))
                self._best_ask = Decimal(str(ask))
                self._last_ticker_time = time.time()
        except Exception as e:
            logger.debug(f"Error parsing GRVT mini ticker: {e}, data={data}")

    async def _on_order_update(self, data: dict):
        """Handle order feed updates for fill detection."""
        try:
            # Extract order data — SDK may wrap in various structures
            payload = data.get("result", data) if isinstance(data, dict) else data
            entries = []
            if isinstance(payload, dict):
                if "feed" in payload and isinstance(payload["feed"], dict):
                    entries.append(payload["feed"])
                if "orders" in payload:
                    orders = payload["orders"]
                    if isinstance(orders, list):
                        entries.extend([o for o in orders if isinstance(o, dict)])
                    elif isinstance(orders, dict):
                        for value in orders.values():
                            if isinstance(value, list):
                                entries.extend([o for o in value if isinstance(o, dict)])
                            elif isinstance(value, dict):
                                entries.append(value)
                if not entries:
                    entries.append(payload)
            elif isinstance(payload, list):
                entries.extend([o for o in payload if isinstance(o, dict)])

            for order in entries:
                client_order_id = str(
                    order.get("client_order_id")
                    or order.get("metadata", {}).get("client_order_id")
                    or ""
                )
                if not client_order_id:
                    continue

                status = str(order.get("status", "")).upper()

                # Log all order updates for debugging
                order_id = order.get("order_id", "?")
                filled = order.get("filled_size", order.get("filled", "0"))
                logger.debug(
                    f"GRVT order update: id={order_id} cid={client_order_id} "
                    f"status={status} filled={filled}"
                )

                if client_order_id not in self._pending_fills:
                    continue

                filled_size = Decimal(str(order.get("filled_size", order.get("filled", "0"))))
                price = Decimal(
                    str(
                        order.get("price")
                        or order.get("avg_price")
                        or order.get("limit_price")
                        or "0"
                    )
                )

                if status == "FILLED":
                    self._fill_results[client_order_id] = {
                        "filled_size": filled_size,
                        "price": price,
                        "status": "FILLED",
                    }
                    self._pending_fills[client_order_id].set()
                    logger.info(f"GRVT fill confirmed: cid={client_order_id} filled={filled_size} @ {price}")

                elif status in ("REJECTED", "CANCELLED", "CANCELED"):
                    self._fill_results[client_order_id] = {
                        "filled_size": filled_size,
                        "price": price,
                        "status": status,
                    }
                    self._pending_fills[client_order_id].set()
                    if filled_size > 0:
                        logger.info(
                            f"GRVT order {status} with partial fill: "
                            f"cid={client_order_id} filled={filled_size}"
                        )
                    else:
                        logger.info(f"GRVT order {status}: cid={client_order_id}")

        except Exception as e:
            logger.error(f"Error handling GRVT order update: {e}, data={data}")

    # ========== Public API ==========

    def get_bbo(self) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        return self._best_bid, self._best_ask

    def is_ws_stale(self) -> bool:
        if self._last_ticker_time == 0:
            return True
        return (time.time() - self._last_ticker_time) > WS_STALE_THRESHOLD

    async def place_post_only_order(self, side: str, price: Decimal, size: Decimal) -> str:
        """Place post-only maker order. Returns client_order_id."""
        if not self._ws:
            raise RuntimeError("GRVT client not connected")

        client_order_id = str(int(time.time() * 1_000_000))

        # Register fill event BEFORE placing order
        fill_event = asyncio.Event()
        self._pending_fills[client_order_id] = fill_event

        try:
            result = await self._ws.rpc_create_order(
                symbol=self._symbol,
                order_type="limit",
                side=side.lower(),
                amount=float(size),
                price=str(price),
                params={
                    "client_order_id": client_order_id,
                    "post_only": True,
                    "time_in_force": "GOOD_TILL_TIME",
                },
            )

            if not result:
                raise RuntimeError("GRVT rpc_create_order returned empty result")

            logger.info(f"GRVT post-only sent: cid={client_order_id} {side} {size} @ {price}")
            return client_order_id

        except Exception as e:
            # Some SDK/network errors happen after the order is already accepted.
            # Keep tracking briefly to avoid losing the fill event in this race.
            recovered = await self._recover_submit_after_error(client_order_id, side, size, price, e)
            if recovered:
                return client_order_id
            raise

    async def _recover_submit_after_error(
        self,
        client_order_id: str,
        side: str,
        size: Decimal,
        price: Decimal,
        error: Exception,
    ) -> bool:
        logger.error(
            f"GRVT submit error for cid={client_order_id} ({side} {size} @ {price}): {error}. "
            f"Waiting {SEND_ERROR_GRACE_TIMEOUT}s for late WS confirmation."
        )
        event = self._pending_fills.get(client_order_id)
        if event is None:
            return False

        try:
            await asyncio.wait_for(event.wait(), timeout=SEND_ERROR_GRACE_TIMEOUT)
            if client_order_id in self._fill_results:
                logger.warning(f"GRVT order likely accepted despite submit error: cid={client_order_id}")
                return True
        except asyncio.TimeoutError:
            pass

        # No WS confirmation in grace window: clean up to avoid stale pending events.
        self._pending_fills.pop(client_order_id, None)
        self._fill_results.pop(client_order_id, None)
        return False

    async def wait_for_fill(
        self,
        client_order_id: str,
        timeout: float,
        keep_pending_on_timeout: bool = False,
    ) -> Optional[dict]:
        """Wait for fill event from WS. Returns fill data or None on timeout."""
        event = self._pending_fills.get(client_order_id)
        if not event:
            # Late fill may already be cached.
            return self._fill_results.pop(client_order_id, None)
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            self._pending_fills.pop(client_order_id, None)
            return self._fill_results.pop(client_order_id, None)
        except asyncio.TimeoutError:
            logger.warning(f"GRVT fill timeout: cid={client_order_id} after {timeout}s")
            if keep_pending_on_timeout:
                return None
            self._pending_fills.pop(client_order_id, None)
            return None

    def clear_pending_fill(self, client_order_id: str) -> None:
        """Clear pending fill event while keeping cached fill result if present."""
        self._pending_fills.pop(client_order_id, None)

    def get_last_fill(self, client_order_id: str) -> Optional[dict]:
        """Get fill result if already available (for checking after cancel)."""
        return self._fill_results.pop(client_order_id, None)

    async def cancel_order(self, client_order_id: str) -> bool:
        """Cancel order by client_order_id."""
        if not self._ws:
            return False
        try:
            result = await self._ws.rpc_cancel_order(
                id=client_order_id,
                params={"client_order_id": client_order_id},
            )
            logger.info(f"GRVT cancel sent: cid={client_order_id}")
            return True
        except Exception as e:
            logger.warning(f"GRVT cancel failed: cid={client_order_id} error={e}")
            return False

    async def cancel_all_orders(self, market_id: str = "") -> None:
        if not self._ws:
            return
        try:
            await self._ws.rpc_cancel_all_orders(
                params={"instrument": self._symbol},
            )
            logger.info(f"GRVT: all orders canceled for {self._symbol}")
        except Exception as e:
            logger.error(f"GRVT cancel_all_orders failed: {e}")

    async def get_position(self, market_id: str = "") -> Decimal:
        if not self._ws:
            raise RuntimeError("GRVT not connected")
        try:
            positions = await self._ws.fetch_positions()
            logger.debug(f"GRVT fetch_positions raw: {positions}")
            if positions:
                for pos in positions:
                    if self._matches_position_symbol(pos):
                        symbol = pos.get("instrument", pos.get("symbol", self._symbol))
                        size = pos.get("size", pos.get("contracts", ""))
                        side = pos.get("side", "")
                        notional = pos.get("notional", "")
                        amount = self._extract_signed_position(pos)
                        logger.debug(
                            "GRVT position matched: "
                            f"symbol={symbol} size={size} side={side} notional={notional} -> {amount}"
                        )
                        return amount
                # No match found — log what we got for debugging
                symbols_found = [pos.get("instrument", pos.get("symbol", "?")) for pos in positions]
                logger.warning(f"GRVT positions exist but no match for {self._symbol}: {symbols_found}")
            return Decimal("0")
        except Exception as e:
            logger.error(f"Failed to get GRVT position: {e}")
            raise

    async def get_balance(self) -> Decimal:
        if not self._ws:
            raise RuntimeError("GRVT not connected")
        try:
            balance = await self._ws.fetch_balance()
            if balance and "free" in balance:
                usdt = balance["free"].get("USDT", balance["free"].get("USD", 0))
                return Decimal(str(usdt))
            return Decimal("0")
        except Exception as e:
            logger.error(f"Failed to get GRVT balance: {e}")
            raise

    def _align_price_to_tick(self, price: Decimal, side: str) -> Decimal:
        """Round price to tick_size boundary.

        GRVT silently rejects orders with non-tick-aligned prices.
        For sell: round DOWN (more aggressive, ensures fill).
        For buy: round UP (more aggressive, ensures fill).
        """
        if side == "sell":
            aligned = (price / self._tick_size).to_integral_value(rounding=ROUND_DOWN) * self._tick_size
        else:
            aligned = (price / self._tick_size).to_integral_value(rounding=ROUND_UP) * self._tick_size
        if aligned != price:
            logger.debug(f"Price aligned to tick: {price} → {aligned} (tick={self._tick_size})")
        return aligned

    async def close_position(self, market_id: str, position: Decimal, slippage_pct: Decimal) -> bool:
        if abs(position) == 0:
            return True

        # Close: sell if long, buy if short
        if position > 0:
            side = "sell"
            if self._best_bid:
                price = self._best_bid * (Decimal("1") - slippage_pct)
            else:
                logger.error("Cannot close GRVT: no bid")
                return False
        else:
            side = "buy"
            if self._best_ask:
                price = self._best_ask * (Decimal("1") + slippage_pct)
            else:
                logger.error("Cannot close GRVT: no ask")
                return False

        # CRITICAL: align price to tick_size — GRVT silently rejects non-aligned prices
        price = self._align_price_to_tick(price, side)

        close_size = abs(position)

        try:
            client_order_id = str(int(time.time() * 1_000_000))
            fill_event = asyncio.Event()
            self._pending_fills[client_order_id] = fill_event

            logger.info(f"GRVT close order: {side} {close_size} @ {price} (tick-aligned)")
            result = await self._ws.rpc_create_order(
                symbol=self._symbol,
                order_type="limit",
                side=side,
                amount=float(close_size),
                price=str(price),
                params={
                    "client_order_id": client_order_id,
                    "time_in_force": "IMMEDIATE_OR_CANCEL",
                    "reduce_only": True,
                },
            )
            logger.info(f"GRVT close rpc result keys: {list(result.keys()) if isinstance(result, dict) else type(result)}")

            # Wait for fill via WS
            try:
                await asyncio.wait_for(fill_event.wait(), timeout=15)
                fill_data = self._fill_results.pop(client_order_id, None)
                if fill_data and fill_data.get("filled_size", Decimal("0")) > 0:
                    logger.info(f"GRVT close filled via WS: {fill_data['filled_size']}")
                    return True
            except asyncio.TimeoutError:
                logger.warning("GRVT close WS fill timeout, checking REST position...")
            finally:
                self._pending_fills.pop(client_order_id, None)

            # REST fallback: check if position actually closed
            try:
                await asyncio.sleep(1)  # let order settle
                remaining = await self.get_position()
                if abs(remaining) < abs(position) * Decimal("0.5"):
                    logger.info(f"GRVT close confirmed via REST: remaining={remaining}")
                    return True
                logger.warning(f"GRVT close not confirmed: remaining={remaining} (was {position})")
            except Exception as e:
                logger.warning(f"GRVT close REST check failed: {e}")

            return False
        except Exception as e:
            logger.error(f"GRVT close_position error: {e}")
            return False

    @property
    def tick_size(self) -> Decimal:
        return self._tick_size

    @property
    def symbol(self) -> str:
        return self._symbol
