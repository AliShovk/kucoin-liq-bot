import asyncio
import logging
from telegram import Bot
from telegram.constants import ParseMode
from config import Config

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Отправка поэтапных сигналов в Telegram."""

    def __init__(self):
        self.bot = Bot(token=Config.TELEGRAM_BOT_TOKEN)
        self.chat_id = Config.TELEGRAM_CHAT_ID

    async def send(self, text: str):
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
            logger.info("TG sent: %s", text[:80])
        except Exception as e:
            logger.error("TG send error: %s", e)

    # ── Этапы сигнала ───────────────────────────────────────────────

    async def stage_1_liquidation(self, symbol: str, side: str, size_usd: float):
        """🟢  — обнаружена крупная ликвидация."""
        direction = "LONG ликвидация 📉" if side == "sell" else "SHORT ликвидация 📈"
        text = (
            f"🟢 <b>Ликвидация обнаружена</b>\n"
            f"{symbol} | {direction}\n"
            f"Объём: <b>${size_usd:,.0f}</b>"
        )
        await self.send(text)

    async def stage_2_bollinger(self, symbol: str, side: str, price: float,
                                 lower: float, upper: float):
        """🟢🟢  — цена коснулась/пробила линию Боллинджера."""
        if side == "buy":
            band_info = f"Цена {price:.2f} ≤ нижняя лента {lower:.2f}"
        else:
            band_info = f"Цена {price:.2f} ≥ верхняя лента {upper:.2f}"
        text = (
            f"🟢🟢 <b>Боллинджер подтверждает</b>\n"
            f"{symbol} | {band_info}"
        )
        await self.send(text)

    async def stage_3_order_cluster(self, symbol: str, side: str,
                                     cluster_price: float, cluster_vol: float):
        """🟢🟢🟢  — обнаружено скопление ордеров на развороте."""
        wall = "BID-стена (покупатели)" if side == "buy" else "ASK-стена (продавцы)"
        text = (
            f"🟢🟢🟢 <b>Скопление ордеров</b>\n"
            f"{symbol} | {wall}\n"
            f"Цена кластера: {cluster_price:.2f}  |  Объём: {cluster_vol:,.2f}"
        )
        await self.send(text)

    async def stage_4_dominance_shift(self, symbol: str, side: str,
                                       buy_pct: float, sell_pct: float):
        """🟢🟢🟢🟢  — перевес покупателей/продавцов подтверждён."""
        if side == "buy":
            shift = f"Покупатели {buy_pct:.0%} > Продавцы {sell_pct:.0%}"
        else:
            shift = f"Продавцы {sell_pct:.0%} > Покупатели {buy_pct:.0%}"
        text = (
            f"🟢🟢🟢🟢 <b>Перевес подтверждён</b>\n"
            f"{symbol} | {shift}"
        )
        await self.send(text)

    async def stage_final_signal(self, symbol: str, side: str, price: float):
        """🎆 ПОКУПКА / ПРОДАЖА — финальный сигнал."""
        action = "🚀 ПОКУПКА (LONG)" if side == "buy" else "🔻 ПРОДАЖА (SHORT)"
        text = (
            f"🎆🎆🎆 <b>СИГНАЛ: {action}</b> 🎆🎆🎆\n"
            f"{symbol} @ {price:.2f}\n"
            f"Все 4 подтверждения пройдены ✅"
        )
        await self.send(text)
