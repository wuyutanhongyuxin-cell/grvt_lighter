import logging
from decimal import Decimal

import aiohttp

logger = logging.getLogger("arbitrage.telegram")

TG_API = "https://api.telegram.org"


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)
        self._session: aiohttp.ClientSession | None = None
        if not self.enabled:
            logger.info("Telegram notifications disabled (missing TG_BOT_TOKEN or TG_CHAT_ID)")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def send_message(self, text: str):
        if not self.enabled:
            return
        try:
            session = await self._get_session()
            url = f"{TG_API}/bot{self.bot_token}/sendMessage"
            payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"}
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"TG send failed [{resp.status}]: {body[:200]}")
        except Exception as e:
            logger.warning(f"TG send error: {e}")

    async def notify_start(self, ticker: str, qty, max_pos, long_thresh, short_thresh):
        text = (
            f"*Arbitrage Bot Started*\n"
            f"Pair: {ticker} | Size: {qty}\n"
            f"Max position: {max_pos}\n"
            f"Long threshold: {long_thresh} | Short threshold: {short_thresh}\n"
            f"GRVT=Maker(post-only) | Lighter=Taker(IOC)"
        )
        await self.send_message(text)

    async def notify_stop(self, reason: str, runtime_hours: float, total_trades: int):
        text = (
            f"*Bot Stopped*\n"
            f"Reason: {reason}\n"
            f"Runtime: {runtime_hours:.1f}h | Trades: {total_trades}"
        )
        await self.send_message(text)

    async def notify_trade(
        self, direction: str,
        grvt_side: str, grvt_price, grvt_size,
        lighter_side: str, lighter_price, lighter_size,
        spread_captured,
        grvt_position, lighter_position,
    ):
        dir_label = "Long GRVT" if direction == "long_grvt" else "Short GRVT"
        profit_est = Decimal(str(spread_captured)) * Decimal(str(grvt_size))
        profit_sign = "+" if profit_est >= 0 else ""
        text = (
            f"*Trade: {dir_label}*\n"
            f"GRVT: {grvt_side.upper()}@{grvt_price} x{grvt_size}\n"
            f"Lighter: {lighter_side.upper()}@{lighter_price} x{lighter_size}\n"
            f"Spread: {spread_captured}\n"
            f"Est. profit: {profit_sign}{profit_est:.4f} USDC\n"
            f"Position: GRVT={grvt_position} Lighter={lighter_position}"
        )
        await self.send_message(text)

    async def notify_heartbeat(
        self, runtime_hours: float, total_trades: int,
        diff_long, diff_short,
        long_trigger, short_trigger,
        grvt_position, lighter_position, net_position,
    ):
        long_gap = long_trigger - diff_long
        short_gap = short_trigger - diff_short
        text = (
            f"*Heartbeat* | Runtime {runtime_hours:.1f}h | Trades: {total_trades}\n"
            f"\n*Long spread*: ${diff_long:.2f} (trigger: ${long_trigger:.2f}, gap: ${long_gap:.2f})\n"
            f"*Short spread*: ${diff_short:.2f} (trigger: ${short_trigger:.2f}, gap: ${short_gap:.2f})\n"
            f"\nGRVT: {grvt_position} | Lighter: {lighter_position} | Net: {net_position}"
        )
        await self.send_message(text)

    async def notify_emergency(self, text: str):
        msg = f"*EMERGENCY*\n{text}"
        await self.send_message(msg)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
