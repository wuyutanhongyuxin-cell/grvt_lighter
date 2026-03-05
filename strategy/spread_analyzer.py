"""
Spread calculation and signal detection — fee-aware with natural spread baseline.

Core formula: net_spread = raw_spread - fee_cost - max(0, natural_spread) > threshold

Features:
  - Fee-aware: accounts for maker rebates / taker costs per execution mode
  - Natural spread: median of rolling window removes baseline bias
  - Warmup: suppresses signals until enough samples collected
  - Persistence: signal must hold for N consecutive ticks (anti-flicker)
"""

import logging
import statistics
from collections import deque
from decimal import Decimal
from typing import Optional, Tuple

logger = logging.getLogger("arbitrage.spread")

ZERO = Decimal("0")


class SpreadAnalyzer:
    def __init__(
        self,
        long_threshold: Decimal,
        short_threshold: Decimal,
        min_spread: Decimal,
        execution_mode: str = "maker_taker",
        grvt_maker_fee: Decimal = Decimal("-0.000004"),
        grvt_taker_fee: Decimal = Decimal("0.00042"),
        lighter_taker_fee: Decimal = ZERO,
        natural_spread_window: int = 300,
        warmup_samples: int = 30,
        persistence_count: int = 3,
    ):
        self.long_threshold = long_threshold
        self.short_threshold = short_threshold
        self.min_spread = min_spread
        self.execution_mode = execution_mode

        # Fee rates
        self._grvt_maker_fee = grvt_maker_fee
        self._grvt_taker_fee = grvt_taker_fee
        self._lighter_taker_fee = lighter_taker_fee

        # Natural spread tracking
        self._raw_long_history: deque[Decimal] = deque(maxlen=natural_spread_window)
        self._raw_short_history: deque[Decimal] = deque(maxlen=natural_spread_window)
        self._natural_spread_long: Decimal = ZERO
        self._natural_spread_short: Decimal = ZERO

        # Warmup
        self._warmup_target = warmup_samples
        self._update_count: int = 0
        self._warmed_up: bool = (warmup_samples == 0)

        # Persistence
        self._persistence_count = persistence_count
        self._long_persist_counter: int = 0
        self._short_persist_counter: int = 0

        # Raw spreads (backward compat)
        self.diff_long: Decimal = ZERO
        self.diff_short: Decimal = ZERO

        # Computed values exposed for dashboard/logging
        self.mid_price: Decimal = ZERO
        self.fee_cost_long: Decimal = ZERO
        self.fee_cost_short: Decimal = ZERO
        self.net_spread_long: Decimal = ZERO
        self.net_spread_short: Decimal = ZERO

    def _compute_fee_costs(self, mid_price: Decimal) -> Tuple[Decimal, Decimal]:
        """Compute per-BTC fee cost based on execution mode."""
        if self.execution_mode == "market_market":
            total_rate = self._grvt_taker_fee + self._lighter_taker_fee
        else:  # maker_taker
            total_rate = self._grvt_maker_fee + self._lighter_taker_fee
        fee = mid_price * total_rate
        return fee, fee  # symmetric for both directions

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

        # Step 1: Raw spreads (unchanged)
        self.diff_long = lighter_bid - grvt_ask
        self.diff_short = grvt_bid - lighter_ask

        # Step 2: Mid price for fee calculation
        self.mid_price = (grvt_bid + grvt_ask + lighter_bid + lighter_ask) / 4

        # Step 3: Fee costs
        self.fee_cost_long, self.fee_cost_short = self._compute_fee_costs(self.mid_price)

        # Step 4: Append to history
        self._raw_long_history.append(self.diff_long)
        self._raw_short_history.append(self.diff_short)
        self._update_count += 1

        # Step 5: Recompute natural spread every ~1s (20 ticks @ 50ms)
        if self._update_count % 20 == 0:
            self._recompute_natural_spread()

        # Step 6: Warmup check
        if not self._warmed_up and self._update_count >= self._warmup_target:
            self._warmed_up = True
            logger.info(
                f"Warmup complete after {self._update_count} samples. "
                f"Natural spread: long=${self._natural_spread_long:.4f} "
                f"short=${self._natural_spread_short:.4f}"
            )

        # Step 7: Net spreads
        self.net_spread_long = (
            self.diff_long - self.fee_cost_long - max(ZERO, self._natural_spread_long)
        )
        self.net_spread_short = (
            self.diff_short - self.fee_cost_short - max(ZERO, self._natural_spread_short)
        )

        # Step 8: Persistence counters
        long_trigger = max(self.long_threshold, self.min_spread)
        short_trigger = max(self.short_threshold, self.min_spread)

        if self.net_spread_long > long_trigger:
            self._long_persist_counter += 1
        else:
            self._long_persist_counter = 0

        if self.net_spread_short > short_trigger:
            self._short_persist_counter += 1
        else:
            self._short_persist_counter = 0

    def _recompute_natural_spread(self):
        """Recompute median of raw spread history."""
        if len(self._raw_long_history) >= 10:
            self._natural_spread_long = Decimal(
                str(statistics.median(self._raw_long_history))
            )
        if len(self._raw_short_history) >= 10:
            self._natural_spread_short = Decimal(
                str(statistics.median(self._raw_short_history))
            )

    def check_signal(self) -> Tuple[Optional[str], Decimal]:
        """
        Check for arbitrage signal with three gates:
          1. Warmup complete
          2. net_spread > trigger
          3. Persistence count met

        Returns (direction, net_spread_value) or (None, 0).
        """
        # Gate 1: Warmup
        if not self._warmed_up:
            return None, ZERO

        long_trigger = max(self.long_threshold, self.min_spread)
        short_trigger = max(self.short_threshold, self.min_spread)

        # Gate 2+3: Net spread exceeds trigger AND persisted enough ticks
        if (
            self.net_spread_long > long_trigger
            and self._long_persist_counter >= self._persistence_count
        ):
            return "long_grvt", self.net_spread_long

        if (
            self.net_spread_short > short_trigger
            and self._short_persist_counter >= self._persistence_count
        ):
            return "short_grvt", self.net_spread_short

        return None, ZERO

    def get_stats(self) -> dict:
        long_trigger = max(self.long_threshold, self.min_spread)
        short_trigger = max(self.short_threshold, self.min_spread)
        return {
            # Original keys (backward compat)
            "diff_long": self.diff_long,
            "diff_short": self.diff_short,
            "long_threshold": self.long_threshold,
            "short_threshold": self.short_threshold,
            "min_spread": self.min_spread,
            "effective_long_trigger": long_trigger,
            "effective_short_trigger": short_trigger,
            "long_gap": long_trigger - self.net_spread_long,
            "short_gap": short_trigger - self.net_spread_short,
            # New keys
            "execution_mode": self.execution_mode,
            "mid_price": self.mid_price,
            "fee_cost_long": self.fee_cost_long,
            "fee_cost_short": self.fee_cost_short,
            "natural_spread_long": self._natural_spread_long,
            "natural_spread_short": self._natural_spread_short,
            "net_spread_long": self.net_spread_long,
            "net_spread_short": self.net_spread_short,
            "warmed_up": self._warmed_up,
            "warmup_progress": min(self._update_count, self._warmup_target),
            "warmup_target": self._warmup_target,
            "persist_long": self._long_persist_counter,
            "persist_short": self._short_persist_counter,
            "persist_required": self._persistence_count,
            "samples_collected": self._update_count,
        }
