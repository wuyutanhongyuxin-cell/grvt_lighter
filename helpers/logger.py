import csv
import logging
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal


def setup_logger(name: str = "arbitrage", level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)-20s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_filename = os.path.join(log_dir, f"arbitrage_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    file_handler = logging.FileHandler(log_filename, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


class DataLogger:
    def __init__(self, ticker: str):
        data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(data_dir, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._bbo_path = os.path.join(data_dir, f"bbo_{ticker}_{ts}.csv")
        self._trade_path = os.path.join(data_dir, f"trades_{ticker}_{ts}.csv")

        self._bbo_file = open(self._bbo_path, "w", newline="", encoding="utf-8")
        self._bbo_writer = csv.writer(self._bbo_file)
        self._bbo_writer.writerow([
            "timestamp", "grvt_bid", "grvt_ask", "lighter_bid", "lighter_ask",
            "diff_long", "diff_short",
        ])

        self._trade_file = open(self._trade_path, "w", newline="", encoding="utf-8")
        self._trade_writer = csv.writer(self._trade_file)
        self._trade_writer.writerow([
            "timestamp", "direction", "grvt_side", "grvt_price", "grvt_size",
            "lighter_side", "lighter_price", "lighter_size", "spread",
            "grvt_position", "lighter_position",
        ])

    def log_bbo(self, grvt_bid, grvt_ask, lighter_bid, lighter_ask, diff_long, diff_short):
        self._bbo_writer.writerow([
            datetime.now(timezone.utc).isoformat(),
            str(grvt_bid), str(grvt_ask),
            str(lighter_bid), str(lighter_ask),
            str(diff_long), str(diff_short),
        ])
        self._bbo_file.flush()

    def log_trade(self, direction, grvt_side, grvt_price, grvt_size,
                  lighter_side, lighter_price, lighter_size, spread,
                  grvt_position, lighter_position, **kwargs):
        self._trade_writer.writerow([
            datetime.now(timezone.utc).isoformat(),
            direction, grvt_side, str(grvt_price), str(grvt_size),
            lighter_side, str(lighter_price), str(lighter_size), str(spread),
            str(grvt_position), str(lighter_position),
        ])
        self._trade_file.flush()

    def close(self):
        if self._bbo_file and not self._bbo_file.closed:
            self._bbo_file.close()
        if self._trade_file and not self._trade_file.closed:
            self._trade_file.close()
