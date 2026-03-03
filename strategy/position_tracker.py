"""
Position tracking with dual-source verification:
local delta tracking + periodic API reconciliation.
"""

import logging
import time
from decimal import Decimal
from typing import Optional

logger = logging.getLogger("arbitrage.position")

RECONCILE_INTERVAL = 60  # seconds between API reconciliation


class PositionTracker:
    def __init__(self, order_quantity: Decimal, max_position: Decimal):
        self.order_quantity = order_quantity
        self.max_position = max_position

        self.grvt_position: Decimal = Decimal("0")
        self.lighter_position: Decimal = Decimal("0")

        self._last_reconcile: float = 0

    def update_grvt(self, side: str, qty: Decimal):
        if side.lower() == "buy":
            self.grvt_position += qty
        else:
            self.grvt_position -= qty
        logger.info(f"Position update: GRVT {side} {qty} → GRVT={self.grvt_position}")

    def update_lighter(self, side: str, qty: Decimal):
        if side.lower() == "buy":
            self.lighter_position += qty
        else:
            self.lighter_position -= qty
        logger.info(f"Position update: Lighter {side} {qty} → Lighter={self.lighter_position}")

    def get_net_position(self) -> Decimal:
        """Net exposure = |grvt + lighter|. Should be near zero for hedged positions."""
        return abs(self.grvt_position + self.lighter_position)

    def can_long_grvt(self) -> bool:
        """Can we buy more on GRVT? (GRVT position below max)"""
        return self.grvt_position < self.max_position

    def can_short_grvt(self) -> bool:
        """Can we sell more on GRVT? (GRVT position above -max)"""
        return self.grvt_position > -self.max_position

    def check_risk(self) -> bool:
        """Returns False if position divergence exceeds safety threshold."""
        net = self.get_net_position()
        if net > self.order_quantity * 3:
            logger.error(
                f"POSITION DIVERGENCE! net={net} (threshold={self.order_quantity * 3}) "
                f"GRVT={self.grvt_position} Lighter={self.lighter_position}"
            )
            return False
        return True

    def check_pre_trade(self) -> bool:
        """Returns False if net position too large for safe trading."""
        net = self.get_net_position()
        if net > self.order_quantity * 2:
            logger.warning(
                f"Pre-trade check failed: net={net} > {self.order_quantity * 2} "
                f"GRVT={self.grvt_position} Lighter={self.lighter_position}"
            )
            return False
        return True

    async def reconcile_with_api(self, grvt_client, lighter_client):
        """Periodically fetch real positions from API and compare with local tracker."""
        now = time.time()
        if now - self._last_reconcile < RECONCILE_INTERVAL:
            return
        self._last_reconcile = now

        try:
            api_grvt = await grvt_client.get_position()
            deviation_grvt = abs(api_grvt - self.grvt_position)
            if deviation_grvt > Decimal("0.0001"):
                logger.warning(
                    f"GRVT position deviation: local={self.grvt_position} API={api_grvt} "
                    f"diff={deviation_grvt}"
                )
                self.grvt_position = api_grvt
        except Exception as e:
            logger.warning(f"Failed to reconcile GRVT position: {e}")

        try:
            api_lighter = await lighter_client.get_position()
            deviation_lighter = abs(api_lighter - self.lighter_position)
            if deviation_lighter > Decimal("0.0001"):
                logger.warning(
                    f"Lighter position deviation: local={self.lighter_position} API={api_lighter} "
                    f"diff={deviation_lighter}"
                )
                self.lighter_position = api_lighter
        except Exception as e:
            logger.warning(f"Failed to reconcile Lighter position: {e}")

    def get_stats(self) -> dict:
        return {
            "grvt_position": self.grvt_position,
            "lighter_position": self.lighter_position,
            "net_position": self.get_net_position(),
        }
