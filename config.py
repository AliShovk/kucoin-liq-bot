import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # KuCoin
    KUCOIN_API_KEY = os.getenv("KUCOIN_API_KEY", "")
    KUCOIN_API_SECRET = os.getenv("KUCOIN_API_SECRET", "")
    KUCOIN_API_PASSPHRASE = os.getenv("KUCOIN_API_PASSPHRASE", "")

    # Telegram
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

    # Мульти-символ: парсим ВСЕ фьючерсные пары
    # Пакетное окно сбора ликвидаций (секунды)
    LIQUIDATION_BATCH_WINDOW = int(os.getenv("LIQUIDATION_BATCH_WINDOW", "10"))
    # Макс. кол-во WS-подписок на одно соединение (KuCoin лимит ~100)
    MAX_WS_SYMBOLS = int(os.getenv("MAX_WS_SYMBOLS", "90"))

    # Bollinger Bands
    BOLLINGER_PERIOD = int(os.getenv("BOLLINGER_PERIOD", "20"))
    BOLLINGER_STD = float(os.getenv("BOLLINGER_STD", "2.0"))

    # Порог ликвидации (в USD) — крупные ликвидации
    LIQUIDATION_THRESHOLD_USD = float(os.getenv("LIQUIDATION_THRESHOLD_USD", "50000"))

    # Скопление ордеров: глубина стакана (уровней) и ratio для кластера
    ORDER_CLUSTER_DEPTH = int(os.getenv("ORDER_CLUSTER_DEPTH", "15"))
    ORDER_CLUSTER_RATIO = float(os.getenv("ORDER_CLUSTER_RATIO", "3.0"))

    # Перевес: окно (секунд) и порог доминирования
    DOMINANCE_SHIFT_WINDOW = int(os.getenv("DOMINANCE_SHIFT_WINDOW", "30"))
    DOMINANCE_SHIFT_THRESHOLD = float(os.getenv("DOMINANCE_SHIFT_THRESHOLD", "0.6"))

