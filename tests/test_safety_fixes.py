import asyncio
import json
import unittest
from decimal import Decimal
from unittest.mock import patch

from config import Config
from exchanges.grvt_client import GrvtClient
from strategy.order_manager import OrderManager
from strategy.spread_analyzer import SpreadAnalyzer

try:
    from exchanges.lighter_client import LighterClient
    LIGHTER_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover
    LighterClient = None
    LIGHTER_IMPORT_ERROR = exc


class DummyPositionTracker:
    grvt_position = Decimal("0")
    lighter_position = Decimal("0")

    def check_pre_trade(self):
        return True

    def can_long_grvt(self):
        return True

    def can_short_grvt(self):
        return True

    def update_grvt(self, side, qty):
        if side == "buy":
            self.grvt_position += qty
        else:
            self.grvt_position -= qty

    def update_lighter(self, side, qty):
        if side == "buy":
            self.lighter_position += qty
        else:
            self.lighter_position -= qty

    def get_net_position(self):
        return abs(self.grvt_position + self.lighter_position)


class DummyDataLogger:
    def log_trade(self, **kwargs):
        return None


class DummyTelegram:
    async def notify_emergency(self, text):
        return None

    async def notify_trade(self, **kwargs):
        return None


class FakeGrvtClientForOrderManager:
    tick_size = Decimal("0.1")

    def __init__(self):
        self.place_calls = 0

    async def get_position(self, market_id: str = ""):
        return Decimal("0")

    def get_bbo(self):
        return Decimal("100"), Decimal("101")

    async def place_post_only_order(self, side: str, price: Decimal, size: Decimal):
        self.place_calls += 1
        return f"cid-{self.place_calls}"

    async def wait_for_fill(self, client_order_id: str, timeout: float, keep_pending_on_timeout: bool = False):
        return {
            "filled_size": Decimal("0.1"),
            "price": Decimal("100"),
            "status": "FILLED",
        }

    async def cancel_order(self, client_order_id: str):
        return True

    def get_last_fill(self, client_order_id: str):
        return None

    def clear_pending_fill(self, client_order_id: str):
        return None


class FakeLighterClientSubmitFail:
    async def get_position(self, market_id: str = ""):
        return Decimal("0")

    async def place_ioc_order(self, side: str, size: Decimal):
        raise RuntimeError("submit failed")

    async def wait_for_fill(self, client_order_index: int, timeout: float):
        return None


class FakeWs:
    def __init__(self, messages=None):
        self._messages = list(messages or [])
        self.sent = []

    async def recv(self):
        if self._messages:
            return self._messages.pop(0)
        raise RuntimeError("stop message loop")

    async def send(self, payload):
        self.sent.append(payload)


class FakeSignerOK:
    def create_auth_token_with_expiry(self, deadline):
        return "token-abc", None


class FakeSignerFail:
    def create_auth_token_with_expiry(self, deadline):
        return None, "token error"


class SafetyFixesTest(unittest.IsolatedAsyncioTestCase):
    async def test_order_manager_rejects_invalid_direction(self):
        manager = OrderManager(
            grvt_client=object(),
            lighter_client=object(),
            positions=DummyPositionTracker(),
            data_logger=DummyDataLogger(),
            telegram=DummyTelegram(),
            order_quantity=Decimal("0.1"),
            fill_timeout=5,
        )
        result = await manager.execute_arb("bad_direction")
        self.assertIsNone(result)
        self.assertFalse(manager._executing)

    def test_config_rejects_non_positive_fill_timeout(self):
        cfg = Config(
            grvt_private_key="k",
            grvt_api_key="k",
            grvt_trading_account_id="k",
            lighter_private_key="k",
            order_quantity=Decimal("1"),
            max_position=Decimal("1"),
            long_threshold=Decimal("1"),
            short_threshold=Decimal("1"),
            fill_timeout=0,
        )
        with self.assertRaises(SystemExit):
            cfg.validate()

    def test_config_rejects_negative_min_spread_and_cooldown(self):
        cfg = Config(
            grvt_private_key="k",
            grvt_api_key="k",
            grvt_trading_account_id="k",
            lighter_private_key="k",
            order_quantity=Decimal("1"),
            max_position=Decimal("1"),
            long_threshold=Decimal("1"),
            short_threshold=Decimal("1"),
            min_spread=Decimal("-0.1"),
            signal_cooldown=Decimal("-1"),
            fill_timeout=1,
        )
        with self.assertRaises(SystemExit):
            cfg.validate()

    def test_spread_analyzer_respects_min_spread_gate(self):
        analyzer = SpreadAnalyzer(
            long_threshold=Decimal("3"),
            short_threshold=Decimal("3"),
            min_spread=Decimal("5"),
        )
        analyzer.update(
            lighter_bid=Decimal("104"),
            lighter_ask=Decimal("105"),
            grvt_bid=Decimal("100"),
            grvt_ask=Decimal("101"),
        )

        direction, spread = analyzer.check_signal()
        self.assertIsNone(direction)
        self.assertEqual(spread, Decimal("0"))

        analyzer.update(
            lighter_bid=Decimal("106"),
            lighter_ask=Decimal("107"),
            grvt_bid=Decimal("100"),
            grvt_ask=Decimal("101"),
        )
        direction, spread = analyzer.check_signal()
        self.assertEqual(direction, "long_grvt")
        self.assertEqual(spread, Decimal("6"))

    def test_grvt_tick_size_parsing(self):
        self.assertEqual(
            GrvtClient._parse_tick_size_from_market({"tick_size": "0.5"}),
            Decimal("0.5"),
        )
        self.assertEqual(
            GrvtClient._parse_tick_size_from_market({"precision": {"price": 2}}),
            Decimal("0.01"),
        )
        self.assertEqual(
            GrvtClient._parse_tick_size_from_market({"precision": {"price": "0.1"}}),
            Decimal("0.1"),
        )
        self.assertIsNone(GrvtClient._parse_tick_size_from_market({"precision": {"price": 20}}))

    async def test_grvt_submit_error_recovery_path(self):
        client = GrvtClient("k", "k", "k", "prod", "BTC")
        cid = "1"
        event = asyncio.Event()
        client._pending_fills[cid] = event
        client._fill_results[cid] = {
            "filled_size": Decimal("1"),
            "price": Decimal("100"),
            "status": "FILLED",
        }
        event.set()
        recovered = await client._recover_submit_after_error(
            cid, "buy", Decimal("1"), Decimal("100"), RuntimeError("send error")
        )
        self.assertTrue(recovered)

    async def test_grvt_submit_error_cleanup_on_timeout(self):
        client = GrvtClient("k", "k", "k", "prod", "BTC")
        cid = "2"
        client._pending_fills[cid] = asyncio.Event()
        with patch("exchanges.grvt_client.SEND_ERROR_GRACE_TIMEOUT", 0.01):
            recovered = await client._recover_submit_after_error(
                cid, "buy", Decimal("1"), Decimal("100"), RuntimeError("send error")
            )
        self.assertFalse(recovered)
        self.assertNotIn(cid, client._pending_fills)
        self.assertNotIn(cid, client._fill_results)

    async def test_grvt_wait_timeout_can_keep_pending_for_cancel_check(self):
        client = GrvtClient("k", "k", "k", "prod", "BTC")
        cid = "late-1"
        client._pending_fills[cid] = asyncio.Event()

        result = await client.wait_for_fill(cid, timeout=0.01, keep_pending_on_timeout=True)
        self.assertIsNone(result)
        self.assertIn(cid, client._pending_fills)

        await client._on_order_update(
            {
                "client_order_id": cid,
                "status": "FILLED",
                "filled_size": "0.1",
                "price": "100",
            }
        )
        late_fill = await client.wait_for_fill(cid, timeout=0.1)
        self.assertIsNotNone(late_fill)
        self.assertEqual(late_fill["filled_size"], Decimal("0.1"))
        self.assertNotIn(cid, client._pending_fills)

    async def test_order_manager_halts_trading_after_hedge_submit_failure(self):
        manager = OrderManager(
            grvt_client=FakeGrvtClientForOrderManager(),
            lighter_client=FakeLighterClientSubmitFail(),
            positions=DummyPositionTracker(),
            data_logger=DummyDataLogger(),
            telegram=DummyTelegram(),
            order_quantity=Decimal("0.1"),
            fill_timeout=5,
        )

        first = await manager.execute_arb("long_grvt")
        self.assertIsNone(first)
        self.assertTrue(manager.is_trading_halted)
        self.assertEqual(manager.halt_reason, "HEDGE_SUBMIT_FAILED")

        grvt = manager.grvt_client
        second = await manager.execute_arb("long_grvt")
        self.assertIsNone(second)
        self.assertEqual(grvt.place_calls, 1)

    def test_lighter_filled_base_amount_normalization(self):
        if LighterClient is None:
            self.skipTest(f"LighterClient import failed: {LIGHTER_IMPORT_ERROR}")
        client = LighterClient("k", 1, 1, "BTC")
        client._base_amount_multiplier = 1_000_000

        client._pending_fill_meta[10] = {"requested_size": Decimal("0.25")}
        self.assertEqual(client._normalize_filled_base_amount("250000", 10), Decimal("0.25"))

        client._pending_fill_meta[11] = {"requested_size": Decimal("5")}
        self.assertEqual(client._normalize_filled_base_amount("5", 11), Decimal("5"))

    async def test_lighter_submit_error_cleanup_on_timeout(self):
        if LighterClient is None:
            self.skipTest(f"LighterClient import failed: {LIGHTER_IMPORT_ERROR}")
        client = LighterClient("k", 1, 1, "BTC")
        idx = 7
        client._register_pending_fill(idx, Decimal("1"))
        with patch("exchanges.lighter_client.SEND_ERROR_GRACE_TIMEOUT", 0.01):
            recovered = await client._recover_submit_after_error(
                idx, "sell", Decimal("1"), Decimal("100"), RuntimeError("send error")
            )
        self.assertFalse(recovered)
        self.assertNotIn(idx, client._pending_fills)
        self.assertNotIn(idx, client._pending_fill_meta)

    async def test_lighter_submit_error_recovery_path(self):
        if LighterClient is None:
            self.skipTest(f"LighterClient import failed: {LIGHTER_IMPORT_ERROR}")
        client = LighterClient("k", 1, 1, "BTC")
        idx = 99
        event = client._register_pending_fill(idx, Decimal("1"))
        client._fill_results[idx] = {"filled_size": Decimal("1"), "avg_price": Decimal("100")}
        event.set()
        recovered = await client._recover_submit_after_error(
            idx, "buy", Decimal("1"), Decimal("100"), RuntimeError("send error")
        )
        self.assertTrue(recovered)

    async def test_subscribe_account_orders_success_and_failure(self):
        if LighterClient is None:
            self.skipTest(f"LighterClient import failed: {LIGHTER_IMPORT_ERROR}")
        ws = FakeWs()
        client = LighterClient("k", 12, 3, "BTC")
        client._market_index = 77

        client._signer = FakeSignerOK()
        ok = await client._subscribe_account_orders(ws)
        self.assertTrue(ok)
        self.assertEqual(len(ws.sent), 1)
        sent_payload = json.loads(ws.sent[0])
        self.assertEqual(sent_payload["type"], "subscribe")
        self.assertEqual(sent_payload["channel"], "account_orders/77/12")
        self.assertEqual(sent_payload["auth"], "token-abc")

        client._signer = FakeSignerFail()
        failed = await client._subscribe_account_orders(ws)
        self.assertFalse(failed)

    async def test_message_loop_sets_account_orders_ready_from_subscribed(self):
        if LighterClient is None:
            self.skipTest(f"LighterClient import failed: {LIGHTER_IMPORT_ERROR}")
        client = LighterClient("k", 1, 1, "BTC")
        client._running = True
        client._market_index = 1
        ws = FakeWs(
            messages=[
                json.dumps({"type": "subscribed", "channel": "account_orders/1/1"}),
            ]
        )
        await client._message_loop(ws)
        self.assertTrue(client.is_account_orders_ready())
        client._running = False

    async def test_message_loop_update_account_orders_normalizes_fill(self):
        if LighterClient is None:
            self.skipTest(f"LighterClient import failed: {LIGHTER_IMPORT_ERROR}")
        client = LighterClient("k", 1, 1, "BTC")
        client._running = True
        client._market_index = 1
        client._base_amount_multiplier = 1000
        client._price_multiplier = 1000
        idx = 42
        client._register_pending_fill(idx, Decimal("1.5"), Decimal("2"))

        ws = FakeWs(
            messages=[
                json.dumps(
                    {
                        "type": "update/account_orders/v2",
                        "orders": {
                            "1": [
                                {
                                    "client_order_index": idx,
                                    "status": "FILLED",
                                    "filled_base_amount": "1500",
                                    "filled_quote_amount": "3000",
                                }
                            ]
                        },
                    }
                )
            ]
        )
        await client._message_loop(ws)
        self.assertTrue(client.is_account_orders_ready())
        self.assertIn(idx, client._fill_results)
        self.assertEqual(client._fill_results[idx]["filled_size"], Decimal("1.5"))
        self.assertEqual(client._fill_results[idx]["avg_price"], Decimal("2"))
        client._running = False
