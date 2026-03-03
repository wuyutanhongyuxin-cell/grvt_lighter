"""
Dual orderbook BBO aggregation — thin facade over both exchange clients.
"""

import logging
from decimal import Decimal
from typing import Optional, Tuple

logger = logging.getLogger("arbitrage.orderbook")


class OrderBookManager:
    def __init__(self, grvt_client, lighter_client):
        self._grvt = grvt_client
        self._lighter = lighter_client

    def get_grvt_bbo(self) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        return self._grvt.get_bbo()

    def get_lighter_bbo(self) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        return self._lighter.get_bbo()

    def is_ready(self) -> bool:
        """Both sides have valid BBO."""
        gb, ga = self.get_grvt_bbo()
        lb, la = self.get_lighter_bbo()
        return all(v is not None for v in (gb, ga, lb, la))

    def is_healthy(self) -> bool:
        """Both WS connections are alive."""
        return not self._grvt.is_ws_stale() and not self._lighter.is_ws_stale()
