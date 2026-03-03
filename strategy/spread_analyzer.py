"""
Spread calculation and signal detection.
Fixed threshold comparing bid-bid / ask-ask (QuantGuy pattern).
"""

import logging
from decimal import Decimal
from typing import Optional, Tuple

logger = logging.getLogger("arbitrage.spread")


class SpreadAnalyzer:
    def __init__(self, long_threshold: Decimal, short_threshold: Decimal):
        self.long_threshold = long_threshold
        self.short_threshold = short_threshold

        self.diff_long: Decimal = Decimal("0")
        self.diff_short: Decimal = Decimal("0")

    def update(
        self,
        lighter_bid: Optional[Decimal],
        lighter_ask: Optional[Decimal],
        grvt_bid: Optional[Decimal],
        grvt_ask: Optional[Decimal],
    ):
        """Update spread values from current BBOs."""
        if any(v is None for v in (lighter_bid, lighter_ask, grvt_bid, grvt_ask)):
            return

        # Long GRVT signal: Lighter willing to buy (bid) higher than GRVT bid
        # → Buy on GRVT (maker), Sell on Lighter (taker)
        self.diff_long = lighter_bid - grvt_bid

        # Short GRVT signal: GRVT ask lower than Lighter ask
        # → Sell on GRVT (maker), Buy on Lighter (taker)
        self.diff_short = grvt_ask - lighter_ask

    def check_signal(self) -> Tuple[Optional[str], Decimal]:
        """
        Check for arbitrage signal.
        Returns (direction, spread_value) or (None, 0).

        direction:
          "long_grvt" → buy on GRVT, sell on Lighter
          "short_grvt" → sell on GRVT, buy on Lighter
        """
        if self.diff_long > self.long_threshold:
            return "long_grvt", self.diff_long

        if self.diff_short > self.short_threshold:
            return "short_grvt", self.diff_short

        return None, Decimal("0")

    def get_stats(self) -> dict:
        return {
            "diff_long": self.diff_long,
            "diff_short": self.diff_short,
            "long_threshold": self.long_threshold,
            "short_threshold": self.short_threshold,
            "long_gap": self.long_threshold - self.diff_long,
            "short_gap": self.short_threshold - self.diff_short,
        }
