"""
Lighter exchange client — Taker side (IOC orders).

Custom WebSocket implementation for orderbook + account_orders,
with asyncio.Event-based fill confirmation.
"""

import asyncio
import json
import logging
import time
from decimal import Decimal
from typing import Callable, Dict, Optional, Tuple

import websockets

import lighter
from lighter import ApiClient, Configuration, SignerClient

from .base import BaseExchangeClient

logger = logging.getLogger("arbitrage.lighter")

# Constants
WS_URL = "wss://mainnet.zklighter.elliot.ai/stream"
BASE_URL = "https://mainnet.zklighter.elliot.ai"
MIN_NOTIONAL_FILTER = 40000  # USD — only trade on levels with this notional or more
IOC_SLIPPAGE_PCT = Decimal("0.002")  # 0.2% slippage for IOC execution
AUTH_TOKEN_LIFETIME = 600  # 10 minutes
AUTH_TOKEN_REFRESH_AT = 480  # Refresh at 8 minutes
WS_STALE_THRESHOLD = 30  # seconds
RECONNECT_BASE_DELAY = 1  # seconds
RECONNECT_MAX_DELAY = 30  # seconds
OB_CLEANUP_INTERVAL = 1000  # messages between cleanups
OB_MAX_LEVELS = 100  # max levels per side


class LighterClient(BaseExchangeClient):
    def __init__(
        self,
        private_key: str,
        account_index: int,
        api_key_index: int,
        ticker: str,
    ):
        self._private_key = private_key
        self._account_index = account_index
        self._api_key_index = api_key_index
        self._ticker = ticker

        # SDK clients
        self._signer: Optional[SignerClient] = None
        self._api_client: Optional[ApiClient] = None

        # Market config (populated in connect)
        self._market_index: Optional[int] = None
        self._base_amount_multiplier: int = 1
        self._price_multiplier: int = 1
        self._tick_size: Decimal = Decimal("0.01")

        # WebSocket state
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._running = False
        self._last_ws_msg_time: float = 0

        # Orderbook state
        # Note: float keys are safe here because the WS API returns consistent
        # string→float conversions for the same price level across updates.
        self._orderbook: Dict[str, Dict[float, float]] = {"bids": {}, "asks": {}}
        self._ob_offset: Optional[int] = None
        self._ob_snapshot_loaded = False
        self._ob_sequence_gap = False
        self._ob_lock = asyncio.Lock()

        # Fill tracking — asyncio.Event per client_order_index
        self._pending_fills: Dict[int, asyncio.Event] = {}
        self._fill_results: Dict[int, dict] = {}

    # ========== Connection ==========

    async def connect(self) -> None:
        # Initialize API client
        self._api_client = ApiClient(configuration=Configuration(host=BASE_URL))

        # Initialize signer client
        self._signer = SignerClient(
            url=BASE_URL,
            private_key=self._private_key,
            account_index=self._account_index,
            api_key_index=self._api_key_index,
        )
        err = self._signer.check_client()
        if err is not None:
            raise RuntimeError(f"Lighter SignerClient check failed: {err}")
        logger.info("Lighter SignerClient initialized")

        # Fetch market config
        await self._fetch_market_config()

        # Start WS loop in background
        self._running = True
        self._ws_task = asyncio.create_task(self._ws_loop())
        logger.info("Lighter WS task started")

        # Wait for orderbook snapshot
        for _ in range(100):  # 10 seconds max
            if self._ob_snapshot_loaded:
                logger.info("Lighter orderbook snapshot loaded")
                return
            await asyncio.sleep(0.1)
        logger.warning("Lighter orderbook snapshot not loaded within 10s, continuing anyway")

    async def disconnect(self) -> None:
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._api_client:
            await self._api_client.close()
        logger.info("Lighter disconnected")

    async def _fetch_market_config(self):
        order_api = lighter.OrderApi(self._api_client)
        order_books = await order_api.order_books()

        for market in order_books.order_books:
            if market.symbol == self._ticker:
                self._market_index = market.market_id
                self._base_amount_multiplier = pow(10, market.supported_size_decimals)
                self._price_multiplier = pow(10, market.supported_price_decimals)

                # Get tick size
                market_summary = await order_api.order_book_details(market_id=market.market_id)
                details = market_summary.order_book_details[0]
                self._tick_size = Decimal("1") / (Decimal("10") ** details.price_decimals)

                logger.info(
                    f"Lighter market config: {self._ticker} index={self._market_index} "
                    f"base_mult={self._base_amount_multiplier} price_mult={self._price_multiplier} "
                    f"tick={self._tick_size}"
                )
                return

        raise RuntimeError(f"Ticker {self._ticker} not found on Lighter")

    # ========== WebSocket Loop ==========

    async def _ws_loop(self):
        reconnect_delay = RECONNECT_BASE_DELAY

        while self._running:
            try:
                await self._reset_orderbook()

                async with websockets.connect(WS_URL) as ws:
                    self._ws = ws
                    reconnect_delay = RECONNECT_BASE_DELAY
                    logger.info("Lighter WS connected")

                    # Subscribe to orderbook
                    await ws.send(json.dumps({
                        "type": "subscribe",
                        "channel": f"order_book/{self._market_index}",
                    }))

                    # Subscribe to account orders (private, needs auth)
                    await self._subscribe_account_orders(ws)

                    # Start auth token rotation in background
                    rotation_task = asyncio.create_task(self._auth_rotation_loop(ws))

                    try:
                        await self._message_loop(ws)
                    finally:
                        rotation_task.cancel()
                        try:
                            await rotation_task
                        except asyncio.CancelledError:
                            pass

            except Exception as e:
                logger.error(f"Lighter WS error: {e}")

            if self._running:
                logger.info(f"Lighter WS reconnecting in {reconnect_delay}s...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, RECONNECT_MAX_DELAY)

    async def _subscribe_account_orders(self, ws):
        try:
            deadline = int(time.time() + AUTH_TOKEN_LIFETIME)
            auth_token, err = self._signer.create_auth_token_with_expiry(deadline)
            if err is not None:
                logger.warning(f"Failed to create auth token: {err}")
                return
            channel = f"account_orders/{self._market_index}/{self._account_index}"
            await ws.send(json.dumps({
                "type": "subscribe",
                "channel": channel,
                "auth": auth_token,
            }))
            logger.info(f"Subscribed to {channel} (auth expires in {AUTH_TOKEN_LIFETIME}s)")
        except Exception as e:
            logger.warning(f"Failed to subscribe account_orders: {e}")

    async def _auth_rotation_loop(self, ws):
        while True:
            await asyncio.sleep(AUTH_TOKEN_REFRESH_AT)
            try:
                logger.info("Rotating Lighter auth token...")
                channel = f"account_orders/{self._market_index}/{self._account_index}"
                await ws.send(json.dumps({"type": "unsubscribe", "channel": channel}))
                await asyncio.sleep(0.5)
                await self._subscribe_account_orders(ws)
                logger.info("Auth token rotated successfully")
            except Exception as e:
                logger.error(f"Auth rotation failed: {e}, forcing WS reconnect")
                break  # Break to trigger reconnect in _ws_loop

    async def _message_loop(self, ws):
        cleanup_counter = 0
        timeout_count = 0

        while self._running:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                timeout_count += 1
                if timeout_count % 30 == 0:
                    logger.warning(f"No Lighter WS message for {timeout_count}s")
                continue
            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"Lighter WS closed: {e}")
                break
            except Exception as e:
                logger.error(f"Lighter WS recv error: {e}")
                break

            timeout_count = 0
            self._last_ws_msg_time = time.time()

            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type", "")

            if msg_type == "ping":
                await ws.send(json.dumps({"type": "pong"}))
                continue

            if msg_type == "subscribed/order_book":
                await self._handle_ob_snapshot(data)
            elif msg_type == "update/order_book" and self._ob_snapshot_loaded:
                need_resync = await self._handle_ob_update(data)
                if need_resync:
                    break  # Reconnect to get fresh snapshot
            elif msg_type == "update/account_orders":
                self._handle_account_orders(data)

            cleanup_counter += 1
            if cleanup_counter >= OB_CLEANUP_INTERVAL:
                self._cleanup_orderbook()
                cleanup_counter = 0

    # ========== Orderbook ==========

    async def _reset_orderbook(self):
        async with self._ob_lock:
            self._orderbook = {"bids": {}, "asks": {}}
            self._ob_offset = None
            self._ob_snapshot_loaded = False
            self._ob_sequence_gap = False

    async def _handle_ob_snapshot(self, data: dict):
        async with self._ob_lock:
            self._orderbook = {"bids": {}, "asks": {}}
            ob = data.get("order_book", {})
            if "offset" in ob:
                self._ob_offset = ob["offset"]

            self._apply_updates("bids", ob.get("bids", []))
            self._apply_updates("asks", ob.get("asks", []))
            self._ob_snapshot_loaded = True

            bid_count = len(self._orderbook["bids"])
            ask_count = len(self._orderbook["asks"])
            logger.info(f"Lighter OB snapshot: {bid_count} bids, {ask_count} asks, offset={self._ob_offset}")

    async def _handle_ob_update(self, data: dict) -> bool:
        """Returns True if reconnect needed."""
        ob = data.get("order_book", {})
        if "offset" not in ob:
            return False

        new_offset = ob["offset"]

        async with self._ob_lock:
            # Validate offset sequence
            if self._ob_offset is not None:
                expected = self._ob_offset + 1
                if new_offset > expected:
                    logger.warning(f"Lighter OB offset gap: expected {expected}, got {new_offset}")
                    return True  # Need reconnect
                elif new_offset < expected:
                    return False  # Stale, ignore

            self._ob_offset = new_offset
            self._apply_updates("bids", ob.get("bids", []))
            self._apply_updates("asks", ob.get("asks", []))

            # Integrity check: best_bid < best_ask
            if self._orderbook["bids"] and self._orderbook["asks"]:
                best_bid = max(self._orderbook["bids"].keys())
                best_ask = min(self._orderbook["asks"].keys())
                if best_bid >= best_ask:
                    logger.warning(f"Lighter OB integrity fail: bid={best_bid} >= ask={best_ask}")
                    return True  # Need reconnect

        return False

    def _apply_updates(self, side: str, updates: list):
        ob = self._orderbook[side]
        for u in updates:
            try:
                price = float(u["price"])
                size = float(u["size"])
                if price <= 0:
                    continue
                if size == 0:
                    ob.pop(price, None)
                elif size > 0:
                    ob[price] = size
            except (KeyError, ValueError, TypeError):
                continue

    def _cleanup_orderbook(self):
        for side, reverse in [("bids", True), ("asks", False)]:
            ob = self._orderbook[side]
            if len(ob) > OB_MAX_LEVELS:
                sorted_items = sorted(ob.items(), key=lambda x: x[0], reverse=reverse)
                ob.clear()
                for price, size in sorted_items[:OB_MAX_LEVELS]:
                    ob[price] = size

    # ========== Account Orders (Fill Detection) ==========

    def _handle_account_orders(self, data: dict):
        orders = data.get("orders", {}).get(str(self._market_index), [])
        for order in orders:
            try:
                client_order_idx = order.get("client_order_index")
                if client_order_idx is None:
                    # Try alternate field name
                    client_order_idx = order.get("client_order_id")
                if client_order_idx is None:
                    continue

                client_order_idx = int(client_order_idx)
                status = str(order.get("status", "")).upper()

                if client_order_idx not in self._pending_fills:
                    continue

                filled_base = order.get("filled_base_amount", "0")
                filled_quote = order.get("filled_quote_amount", "0")
                filled_base_dec = Decimal(str(filled_base))
                filled_quote_dec = Decimal(str(filled_quote))

                avg_price = Decimal("0")
                if filled_base_dec > 0:
                    avg_price = filled_quote_dec / filled_base_dec

                if status == "FILLED" or (status == "CANCELED" and filled_base_dec > 0):
                    self._fill_results[client_order_idx] = {
                        "filled_size": filled_base_dec,
                        "avg_price": avg_price,
                        "status": status,
                        "is_ask": order.get("is_ask", False),
                    }
                    self._pending_fills[client_order_idx].set()
                    logger.info(
                        f"Lighter fill confirmed: idx={client_order_idx} "
                        f"filled={filled_base_dec} @ {avg_price} status={status}"
                    )
                elif status == "CANCELED" and filled_base_dec == 0:
                    self._fill_results[client_order_idx] = {
                        "filled_size": Decimal("0"),
                        "avg_price": Decimal("0"),
                        "status": "CANCELED",
                    }
                    self._pending_fills[client_order_idx].set()
                    logger.warning(f"Lighter order canceled with zero fill: idx={client_order_idx}")

            except Exception as e:
                logger.error(f"Error handling Lighter order update: {e}")

    # ========== Public API ==========

    def get_bbo(self) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        """Get BBO filtered by min notional ($40k)."""
        try:
            bid_levels = [
                (p, s) for p, s in self._orderbook["bids"].items()
                if s * p >= MIN_NOTIONAL_FILTER
            ]
            ask_levels = [
                (p, s) for p, s in self._orderbook["asks"].items()
                if s * p >= MIN_NOTIONAL_FILTER
            ]

            best_bid = Decimal(str(max(bid_levels, key=lambda x: x[0])[0])) if bid_levels else None
            best_ask = Decimal(str(min(ask_levels, key=lambda x: x[0])[0])) if ask_levels else None
            return best_bid, best_ask
        except (ValueError, KeyError):
            return None, None

    def get_bbo_unfiltered(self) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        """Get raw BBO without notional filter."""
        try:
            best_bid = Decimal(str(max(self._orderbook["bids"].keys()))) if self._orderbook["bids"] else None
            best_ask = Decimal(str(min(self._orderbook["asks"].keys()))) if self._orderbook["asks"] else None
            return best_bid, best_ask
        except (ValueError, KeyError):
            return None, None

    def is_orderbook_ready(self) -> bool:
        return self._ob_snapshot_loaded and bool(self._orderbook["bids"]) and bool(self._orderbook["asks"])

    def is_ws_stale(self) -> bool:
        if self._last_ws_msg_time == 0:
            return True
        return (time.time() - self._last_ws_msg_time) > WS_STALE_THRESHOLD

    async def place_ioc_order(self, side: str, size: Decimal) -> int:
        """Place IOC order using sign_create_order + send_tx. Returns client_order_index."""
        if not self._signer:
            raise RuntimeError("Lighter client not initialized")

        is_ask = side.lower() == "sell"

        # Price with slippage for IOC execution
        best_bid, best_ask = self.get_bbo_unfiltered()
        if best_bid is None or best_ask is None:
            raise RuntimeError("Lighter orderbook not available for IOC order")

        if is_ask:
            price = best_bid * (Decimal("1") - IOC_SLIPPAGE_PCT)  # Sell below bid for guaranteed fill
        else:
            price = best_ask * (Decimal("1") + IOC_SLIPPAGE_PCT)  # Buy above ask for guaranteed fill

        client_order_index = int(time.time() * 1_000_000) % 1_000_000_000

        # Register fill event BEFORE sending order
        fill_event = asyncio.Event()
        self._pending_fills[client_order_index] = fill_event

        try:
            tx_info, error = self._signer.sign_create_order(
                market_index=self._market_index,
                client_order_index=client_order_index,
                base_amount=int(size * self._base_amount_multiplier),
                price=int(price * self._price_multiplier),
                is_ask=is_ask,
                order_type=self._signer.ORDER_TYPE_LIMIT,
                time_in_force=self._signer.ORDER_TIME_IN_FORCE_FILL_OR_KILL,
                reduce_only=False,
                trigger_price=0,
            )
            if error is not None:
                self._pending_fills.pop(client_order_index, None)
                raise RuntimeError(f"Lighter sign error: {error}")

            tx_hash = await self._signer.send_tx(
                tx_type=self._signer.TX_TYPE_CREATE_ORDER,
                tx_info=tx_info,
            )
            logger.info(f"Lighter IOC sent: idx={client_order_index} {side} {size} @ {price} tx={tx_hash}")
            return client_order_index

        except Exception as e:
            self._pending_fills.pop(client_order_index, None)
            raise

    async def wait_for_fill(self, client_order_index: int, timeout: float) -> Optional[dict]:
        """Wait for fill confirmation via WS. Returns fill data or None on timeout."""
        event = self._pending_fills.get(client_order_index)
        if not event:
            return None
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return self._fill_results.pop(client_order_index, None)
        except asyncio.TimeoutError:
            logger.warning(f"Lighter fill timeout for idx={client_order_index} after {timeout}s")
            return None
        finally:
            self._pending_fills.pop(client_order_index, None)

    async def get_position(self, market_id: str = "") -> Decimal:
        try:
            account_api = lighter.AccountApi(self._api_client)
            account_data = await account_api.account(by="index", value=str(self._account_index))
            if account_data and account_data.accounts:
                for pos in account_data.accounts[0].positions:
                    if pos.market_id == self._market_index:
                        return Decimal(str(pos.position))
            return Decimal("0")
        except Exception as e:
            logger.error(f"Failed to get Lighter position: {e}")
            raise

    async def get_balance(self) -> Decimal:
        try:
            account_api = lighter.AccountApi(self._api_client)
            account_data = await account_api.account(by="index", value=str(self._account_index))
            if account_data and account_data.accounts:
                return Decimal(str(account_data.accounts[0].free_collateral))
            return Decimal("0")
        except Exception as e:
            logger.error(f"Failed to get Lighter balance: {e}")
            raise

    async def cancel_all_orders(self, market_id: str = "") -> None:
        if not self._signer:
            return
        try:
            # Calculate a far future timestamp for cancellation
            far_future_ms = int((time.time() + 28 * 24 * 3600) * 1000)
            _, _, error = await self._signer.cancel_all_orders(
                market_index=self._market_index,
                timestamp_ms=far_future_ms,
            )
            if error:
                logger.warning(f"Lighter cancel_all_orders error: {error}")
            else:
                logger.info("Lighter: all orders canceled")
        except Exception as e:
            logger.error(f"Lighter cancel_all_orders failed: {e}")

    async def close_position(self, market_id: str, position: Decimal, slippage_pct: Decimal) -> bool:
        if abs(position) == 0:
            return True

        # Determine side: if long, sell to close; if short, buy to close
        if position > 0:
            side = "sell"
            is_ask = True
            best_bid, _ = self.get_bbo_unfiltered()
            if best_bid is None:
                logger.error("Cannot close Lighter: no bid")
                return False
            price = best_bid * (Decimal("1") - slippage_pct)
        else:
            side = "buy"
            is_ask = False
            _, best_ask = self.get_bbo_unfiltered()
            if best_ask is None:
                logger.error("Cannot close Lighter: no ask")
                return False
            price = best_ask * (Decimal("1") + slippage_pct)

        close_size = abs(position)
        client_order_index = int(time.time() * 1_000_000) % 1_000_000_000

        # Register fill event BEFORE sending order (prevent race condition)
        fill_event = asyncio.Event()
        self._pending_fills[client_order_index] = fill_event

        try:
            tx_info, error = self._signer.sign_create_order(
                market_index=self._market_index,
                client_order_index=client_order_index,
                base_amount=int(close_size * self._base_amount_multiplier),
                price=int(price * self._price_multiplier),
                is_ask=is_ask,
                order_type=self._signer.ORDER_TYPE_LIMIT,
                time_in_force=self._signer.ORDER_TIME_IN_FORCE_FILL_OR_KILL,
                reduce_only=True,
                trigger_price=0,
            )
            if error:
                self._pending_fills.pop(client_order_index, None)
                logger.error(f"Lighter close sign error: {error}")
                return False

            tx_hash = await self._signer.send_tx(
                tx_type=self._signer.TX_TYPE_CREATE_ORDER,
                tx_info=tx_info,
            )
            logger.info(f"Lighter close order sent: {side} {close_size} @ {price}")

            # Wait for fill confirmation
            try:
                await asyncio.wait_for(fill_event.wait(), timeout=15)
                result = self._fill_results.pop(client_order_index, None)
                if result and result.get("filled_size", Decimal("0")) > 0:
                    logger.info(f"Lighter close filled: {result['filled_size']}")
                    return True
            except asyncio.TimeoutError:
                logger.warning("Lighter close fill timeout")

            return False
        except Exception as e:
            logger.error(f"Lighter close_position error: {e}")
            return False
        finally:
            self._pending_fills.pop(client_order_index, None)

    @property
    def tick_size(self) -> Decimal:
        return self._tick_size

    @property
    def market_index(self) -> Optional[int]:
        return self._market_index
