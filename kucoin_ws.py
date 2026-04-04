"""KuCoin Futures WebSocket клиент — мульти-символ.

Подписывается на snapshot ДЛЯ ВСЕХ символов (ликвидации).
Динамически подписывается на trades/orderbook для целевого символа
после того, как пакетный анализатор выбрал самую крупную ликвидацию.

Для получения WS-токена используется публичный REST endpoint.
"""

import asyncio
import json
import time
import logging
from typing import Callable, Awaitable

import aiohttp

from config import Config

logger = logging.getLogger(__name__)

WS_TOKEN_URL = "https://api-futures.kucoin.com/api/v1/bullet-public"


async def _get_ws_token() -> tuple[str, str]:
    """Получить WS endpoint и token через REST."""
    async with aiohttp.ClientSession() as session:
        async with session.post(WS_TOKEN_URL) as resp:
            data = await resp.json()
            token = data["data"]["token"]
            endpoint = data["data"]["instanceServers"][0]["endpoint"]
            return endpoint, token


def _extract_symbol(topic: str) -> str:
    """'/contractMarket/execution:XBTUSDTM' → 'XBTUSDTM'."""
    if ":" in topic:
        return topic.split(":")[-1]
    return ""


class KuCoinFuturesWS:
    """Асинхронный WS-клиент для KuCoin Futures — мульти-символ."""

    def __init__(self, symbols: list[str]):
        self.symbols = symbols  # ['XBTUSDTM', 'ETHUSDTM', ...]
        self._on_trade: Callable | None = None
        self._on_orderbook: Callable | None = None
        self._on_liquidation: Callable | None = None
        self._ws = None
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._ping_interval = 18
        self._active_target: str | None = None  # символ с активной подпиской на trades/ob

    # ── Регистрация callback-ов ──────────────────────────────────────

    def on_trade(self, fn: Callable[..., Awaitable]):
        self._on_trade = fn

    def on_orderbook(self, fn: Callable[..., Awaitable]):
        self._on_orderbook = fn

    def on_liquidation(self, fn: Callable[..., Awaitable]):
        self._on_liquidation = fn

    # ── Динамическая подписка на целевой символ ──────────────────────

    async def focus_symbol(self, symbol: str):
        """Подписаться на trades + orderbook для конкретного символа.
        Автоматически отписывается от предыдущего целевого символа.
        """
        if self._ws is None or self._ws.closed:
            return

        # Отписаться от предыдущего
        if self._active_target and self._active_target != symbol:
            await self._unsubscribe(
                f"/contractMarket/execution:{self._active_target}")
            await self._unsubscribe(
                f"/contractMarket/level2Depth50:{self._active_target}")
            logger.info("Unfocused %s", self._active_target)

        if self._active_target == symbol:
            return  # уже подписаны

        # Подписаться на новый
        await self._subscribe(f"/contractMarket/execution:{symbol}")
        await self._subscribe(f"/contractMarket/level2Depth50:{symbol}")
        self._active_target = symbol
        logger.info("Focused → %s (trades + orderbook)", symbol)

    async def unfocus(self):
        """Отписаться от текущего целевого символа."""
        if self._active_target and self._ws and not self._ws.closed:
            await self._unsubscribe(
                f"/contractMarket/execution:{self._active_target}")
            await self._unsubscribe(
                f"/contractMarket/level2Depth50:{self._active_target}")
            logger.info("Unfocused %s", self._active_target)
            self._active_target = None

    # ── Основной цикл ────────────────────────────────────────────────

    async def run(self):
        self._running = True
        while self._running:
            try:
                await self._connect_and_listen()
            except Exception as e:
                logger.error("WS error: %s — reconnecting in 5s", e)
                await asyncio.sleep(5)

    async def stop(self):
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()

    async def _connect_and_listen(self):
        endpoint, token = await _get_ws_token()
        connect_id = str(int(time.time() * 1000))
        url = f"{endpoint}?token={token}&connectId={connect_id}"

        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(url, heartbeat=self._ping_interval)
        logger.info("WS connected to %s", endpoint)

        # Подписываемся на snapshot (ликвидации) для ВСЕХ символов пачками
        # KuCoin позволяет до 100 символов через запятую в одном topic
        batch_size = Config.MAX_WS_SYMBOLS
        for i in range(0, len(self.symbols), batch_size):
            batch = self.symbols[i : i + batch_size]
            topic = "/contractMarket/snapshot:" + ",".join(batch)
            await self._subscribe(topic)
            await asyncio.sleep(0.2)  # не спамить подписками

        logger.info("Subscribed to snapshots for %d symbols", len(self.symbols))

        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await self._dispatch(msg.data)
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                logger.warning("WS closed/error")
                break

        await self._session.close()

    async def _subscribe(self, topic: str, private: bool = False):
        payload = {
            "id": str(int(time.time() * 1000)),
            "type": "subscribe",
            "topic": topic,
            "privateChannel": private,
            "response": True,
        }
        await self._ws.send_json(payload)
        logger.debug("Subscribed → %s", topic[:80])

    async def _unsubscribe(self, topic: str, private: bool = False):
        payload = {
            "id": str(int(time.time() * 1000)),
            "type": "unsubscribe",
            "topic": topic,
            "privateChannel": private,
            "response": True,
        }
        await self._ws.send_json(payload)
        logger.debug("Unsubscribed → %s", topic[:80])

    async def _dispatch(self, raw: str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = data.get("type")
        if msg_type in ("pong", "welcome", "ack"):
            return

        topic: str = data.get("topic", "")
        payload = data.get("data", {})
        symbol = _extract_symbol(data.get("subject", topic))

        # ── Сделки (execution) ───────────────────────────────────────
        if "execution" in topic and self._on_trade:
            payload["_symbol"] = symbol
            await self._on_trade(payload)

        # ── Стакан L2 depth ──────────────────────────────────────────
        elif "level2Depth50" in topic and self._on_orderbook:
            payload["_symbol"] = symbol
            await self._on_orderbook(payload)

        # ── Snapshot — ликвидации для всех символов ──────────────────
        elif "snapshot" in topic and self._on_liquidation:
            payload["_symbol"] = symbol
            await self._on_liquidation(payload)
