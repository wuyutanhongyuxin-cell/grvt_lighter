"""
Lighter + GRVT Cross-Exchange Arbitrage — Entry Point

GRVT = Maker (post-only) | Lighter = Taker (IOC)
WS event-driven fill confirmation on both sides.
"""

import asyncio
import logging
import signal
import sys

from config import Config, parse_args
from helpers.logger import setup_logger
from strategy.arb_strategy import ArbStrategy

SHUTDOWN_TIMEOUT = 300  # seconds


async def main() -> int:
    args = parse_args()
    config = Config.from_env_and_args(args)

    root_logger = setup_logger("arbitrage", config.log_level, dashboard_mode=not config.no_dashboard)
    logger = logging.getLogger("arbitrage.main")

    logger.info("=" * 60)
    logger.info("Lighter + GRVT Cross-Exchange Arbitrage")
    logger.info(f"Ticker: {config.ticker} | Size: {config.order_quantity} | Max Pos: {config.max_position}")
    logger.info(
        f"Thresholds: long={config.long_threshold} short={config.short_threshold} "
        f"min_spread={config.min_spread} cooldown={config.signal_cooldown}s"
    )
    logger.info(f"Fill timeout: {config.fill_timeout}s")
    logger.info("=" * 60)

    strategy = ArbStrategy(config)
    main_task = asyncio.current_task()
    signal_state = {"count": 0}

    # Signal handling
    def signal_handler(sig, frame):
        signal_state["count"] += 1
        if signal_state["count"] == 1:
            logger.info(f"Received signal {sig}, requesting graceful stop...")
            strategy.request_stop("signal")
            if main_task and not main_task.done():
                main_task.cancel()
            return
        logger.warning(f"Received signal {sig} again, forcing immediate interrupt")
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, signal_handler)
    try:
        signal.signal(signal.SIGTERM, signal_handler)
    except (OSError, AttributeError):
        pass  # SIGTERM not available on Windows

    try:
        await strategy.initialize()
        await strategy.run()
    except asyncio.CancelledError:
        logger.info("Main task cancelled by signal")
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
    finally:
        # During shutdown, allow Ctrl+C to force abort.
        signal.signal(signal.SIGINT, signal.default_int_handler)
        try:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
        except (OSError, AttributeError):
            pass

        logger.info(f"Starting graceful shutdown (timeout={SHUTDOWN_TIMEOUT}s)...")
        try:
            await asyncio.wait_for(strategy.shutdown(), timeout=SHUTDOWN_TIMEOUT)
        except asyncio.TimeoutError:
            logger.error(f"Shutdown timed out after {SHUTDOWN_TIMEOUT}s! MANUAL CHECK REQUIRED!")
        except Exception as e:
            logger.error(f"Shutdown error: {e}", exc_info=True)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
