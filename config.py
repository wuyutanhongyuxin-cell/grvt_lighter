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
    fill_timeout: int = 5  # GRVT maker fill timeout (seconds)
    log_level: str = "INFO"

    @classmethod
    def from_env_and_args(cls, args: argparse.Namespace) -> "Config":
        env_path = Path(__file__).parent / ".env"
        load_dotenv(env_path)

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
            fill_timeout=args.fill_timeout,
            log_level=args.log_level,
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
        if self.fill_timeout <= 0:
            errors.append("--fill-timeout must be positive")
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
    parser.add_argument("--fill-timeout", type=int, default=5, help="GRVT maker fill timeout in seconds (default: 5)")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Log level")
    return parser.parse_args()
