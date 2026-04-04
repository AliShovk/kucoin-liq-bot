"""KuCoin Futures WebSocket клиент.

Подписывается на:
  - /contractMarket/execution:{symbol}    — лента сделок (trades)
  - /contractMarket/level2:{symbol}       — стакан ордеров (orderbook L2)
  - /contract/announcement                — ликвидации (forcedOrder)

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


class KuCoinFuturesWS:
    """Асинхронный WS-клиент для KuCoin Futures."""

    def __init__(self):
        self.symbol = Config.KUCOIN_FUTURES_SYMBOL  # e.g. BTC-USDT
        self._on_trade: Callable | None = None
        self._on_orderbook: Callable | None = None
        self._on_liquidation: Callable | None = None
        self._ws = None
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._ping_interval = 18  # KuCoin требует ping каждые 20с

    # ── Регистрация callback-ов ──────────────────────────────────────

    def on_trade(self, fn: Callable[..., Awaitable]):
        self._on_trade = fn

    def on_orderbook(self, fn: Callable[..., Awaitable]):
        self._on_orderbook = fn

    def on_liquidation(self, fn: Callable[..., Awaitable]):
        self._on_liquidation = fn

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

        # Подписки
        await self._subscribe(f"/contractMarket/execution:{self.symbol}", False)
        await self._subscribe(f"/contractMarket/level2Depth50:{self.symbol}", False)
        # Ликвидации — topic для принудительных ордеров
        await self._subscribe(f"/contractMarket/snapshot:{self.symbol}", False)

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
        logger.info("Subscribed → %s", topic)

    async def _dispatch(self, raw: str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = data.get("type")
        if msg_type == "pong" or msg_type == "welcome" or msg_type == "ack":
            return

        topic: str = data.get("topic", "")
        payload = data.get("data", {})

        # ── Сделки (execution) ───────────────────────────────────────
        if "execution" in topic and self._on_trade:
            await self._on_trade(payload)

        # ── Стакан L2 depth ──────────────────────────────────────────
        elif "level2Depth50" in topic and self._on_orderbook:
            await self._on_orderbook(payload)

        # ── Snapshot — содержит данные о ликвидациях / маркировке ─────
        elif "snapshot" in topic and self._on_liquidation:
            await self._on_liquidation(payload)
