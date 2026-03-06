import argparse
import os
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Config:
    # GRVT
    grvt_private_key: str = ""
    grvt_api_key: str = ""
    grvt_trading_account_id: str = ""
    grvt_env: str = "prod"  # "prod" or "testnet"

    # Lighter
    lighter_private_key: str = ""
    lighter_account_index: int = 0
    lighter_api_key_index: int = 0

    # Telegram (optional)
    tg_bot_token: str = ""
    tg_chat_id: str = ""

    # Strategy (from CLI)
    ticker: str = "BTC"
    order_quantity: Decimal = Decimal("0")
    max_position: Decimal = Decimal("0")
    long_threshold: Decimal = Decimal("10")
    short_threshold: Decimal = Decimal("10")
    min_spread: Decimal = Decimal("0")
    signal_cooldown: Decimal = Decimal("0")
    fill_timeout: int = 1  # GRVT maker fill timeout (seconds) — 1s single-shot to minimize adverse selection
    post_only_max_retries: int = 1  # single attempt, no retry — adverse selection worsens with each retry
    execution_mode: str = "maker_taker"  # "maker_taker" or "market_market"
    log_level: str = "INFO"
    no_dashboard: bool = False

    # Fee parameters
    grvt_maker_fee: Decimal = Decimal("-0.000004")   # -0.0004% (rebate)
    grvt_taker_fee: Decimal = Decimal("0.00042")     # 0.042%
    lighter_taker_fee: Decimal = Decimal("0")         # 0% (Standard)

    # Spread analysis
    natural_spread_window: int = 300    # sliding window sample count (~5min @1/s)
    warmup_samples: int = 30            # warmup sample count (~30s)
    persistence_count: int = 3          # signal must persist N ticks (N×50ms)

    # Strategy selection
    strategy: str = "arb"

    # Mean Reversion
    mr_window: int = 300              # rolling window sample count (~5min @1sample/s)
    mr_entry_z: Decimal = Decimal("2.0")
    mr_exit_z: Decimal = Decimal("0.5")
    mr_stop_z: Decimal = Decimal("4.0")
    mr_maker_timeout: int = 60       # GRVT maker fill wait timeout (seconds)
    mr_warmup: int = 60              # warmup sample count

    # Funding Rate
    fr_check_interval: int = 60      # rate check interval (seconds)
    fr_min_diff: Decimal = Decimal("0.0001")  # minimum rate diff (0.01%)
    fr_hold_min_hours: Decimal = Decimal("1") # minimum holding period (hours)

    @classmethod
    def from_env_and_args(cls, args: argparse.Namespace) -> "Config":
        env_path = Path(__file__).parent / ".env"
        load_dotenv(env_path, override=True)

        config = cls(
            # GRVT
            grvt_private_key=os.getenv("GRVT_PRIVATE_KEY", ""),
            grvt_api_key=os.getenv("GRVT_API_KEY", ""),
            grvt_trading_account_id=os.getenv("GRVT_TRADING_ACCOUNT_ID", ""),
            grvt_env=os.getenv("GRVT_ENV", "prod"),
            # Lighter
            lighter_private_key=os.getenv("LIGHTER_API_KEY_PRIVATE_KEY", ""),
            lighter_account_index=int(os.getenv("LIGHTER_ACCOUNT_INDEX", "0")),
            lighter_api_key_index=int(os.getenv("LIGHTER_API_KEY_INDEX", "0")),
            # Telegram
            tg_bot_token=os.getenv("TG_BOT_TOKEN", ""),
            tg_chat_id=os.getenv("TG_CHAT_ID", ""),
            # Strategy
            ticker=args.ticker,
            order_quantity=Decimal(args.size),
            max_position=Decimal(args.max_position),
            long_threshold=Decimal(args.long_threshold),
            short_threshold=Decimal(args.short_threshold),
            min_spread=Decimal(args.min_spread),
            signal_cooldown=Decimal(args.signal_cooldown),
            fill_timeout=args.fill_timeout,
            post_only_max_retries=args.post_only_retries,
            execution_mode=args.mode,
            log_level=args.log_level,
            no_dashboard=args.no_dashboard,
            # Fees
            grvt_maker_fee=Decimal(args.grvt_maker_fee),
            grvt_taker_fee=Decimal(args.grvt_taker_fee),
            lighter_taker_fee=Decimal(args.lighter_taker_fee),
            # Spread analysis
            natural_spread_window=args.natural_spread_window,
            warmup_samples=args.warmup_samples,
            persistence_count=args.persistence_count,
            # Strategy selection
            strategy=args.strategy,
            # Mean Reversion
            mr_window=args.mr_window,
            mr_entry_z=Decimal(args.mr_entry_z),
            mr_exit_z=Decimal(args.mr_exit_z),
            mr_stop_z=Decimal(args.mr_stop_z),
            mr_maker_timeout=args.mr_maker_timeout,
            mr_warmup=args.mr_warmup,
            # Funding Rate
            fr_check_interval=args.fr_check_interval,
            fr_min_diff=Decimal(args.fr_min_diff),
            fr_hold_min_hours=Decimal(args.fr_hold_min_hours),
        )

        config.validate()
        return config

    def validate(self):
        errors = []
        if not self.grvt_private_key:
            errors.append("GRVT_PRIVATE_KEY is required")
        if not self.grvt_api_key:
            errors.append("GRVT_API_KEY is required")
        if not self.grvt_trading_account_id:
            errors.append("GRVT_TRADING_ACCOUNT_ID is required")
        if not self.lighter_private_key:
            errors.append("LIGHTER_API_KEY_PRIVATE_KEY is required")
        if self.order_quantity <= 0:
            errors.append("--size must be positive")
        if self.max_position <= 0:
            errors.append("--max-position must be positive")
        if self.long_threshold <= 0:
            errors.append("--long-threshold must be positive")
        if self.short_threshold <= 0:
            errors.append("--short-threshold must be positive")
        if self.min_spread < 0:
            errors.append("--min-spread must be non-negative")
        if self.signal_cooldown < 0:
            errors.append("--signal-cooldown must be non-negative")
        if self.fill_timeout <= 0:
            errors.append("--fill-timeout must be positive")
        if self.post_only_max_retries < 1:
            errors.append("--post-only-retries must be >= 1")
        if self.natural_spread_window <= 0:
            errors.append("--natural-spread-window must be positive")
        if self.warmup_samples < 0:
            errors.append("--warmup-samples must be non-negative")
        if self.persistence_count < 1:
            errors.append("--persistence-count must be >= 1")
        # Mean Reversion validation
        if self.strategy == "mean_reversion":
            if self.mr_entry_z <= self.mr_exit_z:
                errors.append("--mr-entry-z must be > --mr-exit-z")
            if self.mr_exit_z <= 0:
                errors.append("--mr-exit-z must be > 0")
            if self.mr_stop_z <= self.mr_entry_z:
                errors.append("--mr-stop-z must be > --mr-entry-z")
        if errors:
            for e in errors:
                print(f"Config error: {e}", file=sys.stderr)
            sys.exit(1)

    @property
    def has_telegram(self) -> bool:
        return bool(self.tg_bot_token and self.tg_chat_id)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lighter + GRVT Cross-Exchange Arbitrage")
    parser.add_argument("--ticker", default="BTC", help="Trading instrument (default: BTC)")
    parser.add_argument("--size", required=True, help="Order quantity per trade")
    parser.add_argument("--max-position", required=True, help="Max single-side position")
    parser.add_argument("--long-threshold", default="10", help="Long signal threshold in USD (default: 10)")
    parser.add_argument("--short-threshold", default="10", help="Short signal threshold in USD (default: 10)")
    parser.add_argument("--min-spread", default="0", help="Global minimum spread gate in USD (default: 0)")
    parser.add_argument("--signal-cooldown", default="0", help="Signal cooldown in seconds (default: 0)")
    parser.add_argument("--fill-timeout", type=int, default=1, help="GRVT maker fill timeout in seconds (default: 1)")
    parser.add_argument("--post-only-retries", type=int, default=1, help="Max post-only order retry attempts (default: 1)")
    parser.add_argument("--mode", default="maker_taker", choices=["maker_taker", "market_market"],
                        help="Execution mode (default: maker_taker)")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Log level")
    parser.add_argument("--no-dashboard", action="store_true", help="Disable rich terminal dashboard, use plain log output")
    # Fee parameters
    parser.add_argument("--grvt-maker-fee", default="-0.000004", help="GRVT maker fee rate (default: -0.000004 = -0.0004%% rebate)")
    parser.add_argument("--grvt-taker-fee", default="0.00042", help="GRVT taker fee rate (default: 0.00042 = 0.042%%)")
    parser.add_argument("--lighter-taker-fee", default="0", help="Lighter taker fee rate (default: 0)")
    # Spread analysis
    parser.add_argument("--natural-spread-window", type=int, default=300, help="Natural spread sliding window size (default: 300)")
    parser.add_argument("--warmup-samples", type=int, default=30, help="Warmup sample count before trading (default: 30)")
    parser.add_argument("--persistence-count", type=int, default=3, help="Signal persistence count required (default: 3)")
    # Strategy selection
    parser.add_argument("--strategy", default="arb", choices=["arb", "mean_reversion", "funding_rate"],
                        help="Strategy mode (default: arb)")
    # Mean Reversion
    parser.add_argument("--mr-window", type=int, default=300, help="MR rolling window sample count (default: 300)")
    parser.add_argument("--mr-entry-z", default="2.0", help="MR entry z-score threshold (default: 2.0)")
    parser.add_argument("--mr-exit-z", default="0.5", help="MR exit z-score threshold (default: 0.5)")
    parser.add_argument("--mr-stop-z", default="4.0", help="MR stop-loss z-score threshold (default: 4.0)")
    parser.add_argument("--mr-maker-timeout", type=int, default=60, help="MR GRVT maker fill timeout in seconds (default: 60)")
    parser.add_argument("--mr-warmup", type=int, default=60, help="MR warmup sample count (default: 60)")
    # Funding Rate
    parser.add_argument("--fr-check-interval", type=int, default=60, help="FR rate check interval in seconds (default: 60)")
    parser.add_argument("--fr-min-diff", default="0.0001", help="FR minimum rate diff threshold (default: 0.0001)")
    parser.add_argument("--fr-hold-min-hours", default="1", help="FR minimum holding period in hours (default: 1)")
    return parser.parse_args()
