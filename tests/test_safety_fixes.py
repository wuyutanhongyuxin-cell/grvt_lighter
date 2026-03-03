import asyncio
import json
import unittest
from decimal import Decimal
from unittest.mock import patch

from config import Config
from exchanges.grvt_client import GrvtClient
from strategy.order_manager import OrderManager

try:
    from exchanges.lighter_client import LighterClient
    LIGHTER_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover
    LighterClient = None
    LIGHTER_IMPORT_ERROR = exc


class DummyPositionTracker:
    pass


class DummyDataLogger:
    pass


class DummyTelegram:
    pass


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
