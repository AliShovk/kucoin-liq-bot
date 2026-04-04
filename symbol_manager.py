"""Получение списка всех активных фьючерсных пар KuCoin."""

import logging
import aiohttp

logger = logging.getLogger(__name__)

CONTRACTS_URL = "https://api-futures.kucoin.com/api/v1/contracts/active"


async def fetch_all_symbols() -> list[str]:
    """Возвращает список символов вида ['XBTUSDTM', 'ETHUSDTM', ...].

    KuCoin Futures использует суффикс M для perpetual контрактов.
    Фильтруем только USDT-маржинальные.
    """
    symbols: list[str] = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(CONTRACTS_URL) as resp:
                data = await resp.json()
                for contract in data.get("data", []):
                    symbol = contract.get("symbol", "")
                    settle = contract.get("settleCurrency", "")
                    status = contract.get("status", "")
                    # Только активные USDT-маржинальные контракты
                    if settle == "USDT" and status == "Open":
                        symbols.append(symbol)
        logger.info("Fetched %d active USDT futures symbols", len(symbols))
    except Exception as e:
        logger.error("Failed to fetch symbols: %s", e)
    return symbols


def symbol_to_display(symbol: str) -> str:
    """XBTUSDTM → XBT/USDT, ETHUSDTM → ETH/USDT."""
    base = symbol.replace("USDTM", "").replace("USDM", "")
    if base == "XBT":
        base = "BTC"
    return f"{base}/USDT"
