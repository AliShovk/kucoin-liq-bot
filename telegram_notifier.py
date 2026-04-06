import logging
from telegram import Bot
from telegram.constants import ParseMode
from config import Config

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Отправка сигналов и торговых событий в Telegram."""

    def __init__(self, use_trade_channel: bool = False):
        token = Config.TRADE_TELEGRAM_BOT_TOKEN if use_trade_channel else Config.TELEGRAM_BOT_TOKEN
        chat_id = Config.TRADE_TELEGRAM_CHAT_ID if use_trade_channel else Config.TELEGRAM_CHAT_ID
        self.bot = Bot(token=token)
        self.chat_id = chat_id

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

    async def stage_1_liquidation(self, symbol: str, side: str, size_usd: float,
                                   batch_count: int = 1, batch_summary: str = ""):
        direction = "LONG ликвидация 📉" if side == "sell" else "SHORT ликвидация 📈"
        batch_info = f"\n📦 Пакет: {batch_summary}" if batch_summary else ""
        text = (
            f"🟢 <b>Ликвидация обнаружена</b>\n"
            f"{symbol} | {direction}\n"
            f"Объём: <b>${size_usd:,.0f}</b> (крупнейшая из {batch_count})"
            f"{batch_info}"
        )
        await self.send(text)

    async def stage_2_reclaim(self, symbol: str, side: str,
                              extreme: float, price: float):
        if side == "buy":
            info = f"Экстремум выноса: {extreme:.2f} → цена вернулась: {price:.2f} ↑"
        else:
            info = f"Экстремум выноса: {extreme:.2f} → цена вернулась: {price:.2f} ↓"
        text = (
            f"🟢🟢 <b>Reclaim подтверждён</b>\n"
            f"{symbol} | {info}\n"
            f"Вынос поглощён, цена возвращается"
        )
        await self.send(text)

    async def stage_3_bollinger(self, symbol: str, side: str, price: float,
                                 lower: float, upper: float):
        if side == "buy":
            band_info = f"Цена {price:.2f} ≤ нижняя лента {lower:.2f}"
        else:
            band_info = f"Цена {price:.2f} ≥ верхняя лента {upper:.2f}"
        text = (
            f"🟢🟢🟢 <b>Боллинджер подтверждает</b>\n"
            f"{symbol} | {band_info}"
        )
        await self.send(text)

    async def stage_4_order_cluster(self, symbol: str, side: str,
                                     cluster_price: float, cluster_vol: float):
        wall = "BID-стена (покупатели)" if side == "buy" else "ASK-стена (продавцы)"
        text = (
            f"🟢🟢🟢🟢 <b>Скопление ордеров</b>\n"
            f"{symbol} | {wall}\n"
            f"Цена кластера: {cluster_price:.2f}  |  Объём: {cluster_vol:,.2f}"
        )
        await self.send(text)

    async def stage_5_dominance_shift(self, symbol: str, side: str,
                                       buy_pct: float, sell_pct: float):
        if side == "buy":
            shift = f"Покупатели {buy_pct:.0%} > Продавцы {sell_pct:.0%}"
        else:
            shift = f"Продавцы {sell_pct:.0%} > Покупатели {buy_pct:.0%}"
        text = (
            f"🟢🟢🟢🟢🟢 <b>Перевес подтверждён</b>\n"
            f"{symbol} | {shift}"
        )
        await self.send(text)

    async def stage_final_signal(self, symbol: str, side: str, price: float):
        action = "🚀 ПОКУПКА (LONG)" if side == "buy" else "🔻 ПРОДАЖА (SHORT)"
        text = (
            f"🎆🎆🎆 <b>СИГНАЛ: {action}</b> 🎆🎆🎆\n"
            f"{symbol} @ {price:.2f}\n"
            f"Все 5 подтверждений пройдены ✅"
        )
        await self.send(text)

    async def trade_opened(self, symbol: str, side: str, size_usdt: float,
                            leverage: int, entry_price: float,
                            tp_price: float, sl_price: float):
        text = (
            f"💰 <b>Позиция открыта</b>\n"
            f"{symbol} | {side.upper()}\n"
            f"Размер: <b>${size_usdt:.2f}</b> | Плечо: <b>{leverage}x</b>\n"
            f"Вход: {entry_price:.2f}\n"
            f"TP: {tp_price:.2f}\n"
            f"SL: {sl_price:.2f}"
        )
        await self.send(text)

    async def trade_open_failed(self, symbol: str, side: str, price: float, reason: str):
        text = (
            f"⚠️ <b>Ошибка открытия позиции</b>\n"
            f"{symbol} | {side.upper()} @ {price:.2f}\n"
            f"Причина: {reason}"
        )
        await self.send(text)

    async def trade_closed(self, symbol: str, side: str, exit_price: float,
                            pnl_pct: float, reason: str, summary: str):
        icon = "✅" if pnl_pct >= 0 else "🛑"
        title = "Take Profit" if reason == "tp" else "Stop Loss" if reason == "sl" else "Позиция закрыта"
        text = (
            f"{icon} <b>{title}</b>\n"
            f"{symbol} | {side.upper()}\n"
            f"Выход: {exit_price:.2f}\n"
            f"PnL: <b>{pnl_pct:.2f}%</b>\n"
            f"📊 {summary}"
        )
        await self.send(text)

    async def trade_size_scaled(self, old_size: float, new_size: float, winrate: float):
        text = (
            f"📈 <b>Размер позиции увеличен</b>\n"
            f"Winrate: {winrate:.1f}%\n"
            f"Размер: ${old_size:.2f} → <b>${new_size:.2f}</b>"
        )
        await self.send(text)
