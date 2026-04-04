import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Telegram
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    TELEGRAM_CHANNEL = os.getenv("TELEGRAM_CHANNEL", "")

    # Bybit — ликвидации
    # Минимальный объём ликвидации (USD) для попадания в агрегатор
    BYBIT_MIN_SIZE = float(os.getenv("BYBIT_MIN_SIZE", "1000"))
    # Порог LONG ликвидаций (суммарный USD за окно) для сигнала
    BYBIT_LONG_THRESHOLD = float(os.getenv("BYBIT_LONG_THRESHOLD", "50000"))
    # Порог SHORT ликвидаций (суммарный USD за окно) для сигнала
    BYBIT_SHORT_THRESHOLD = float(os.getenv("BYBIT_SHORT_THRESHOLD", "50000"))
    # Интервал агрегации ликвидаций (секунды)
    BYBIT_AGGREGATION_INTERVAL = int(os.getenv("BYBIT_AGGREGATION_INTERVAL", "10"))

    # Bollinger Bands
    BOLLINGER_PERIOD = int(os.getenv("BOLLINGER_PERIOD", "20"))
    BOLLINGER_STD = float(os.getenv("BOLLINGER_STD", "2.0"))

    # Скопление ордеров: глубина стакана (уровней) и ratio для кластера
    ORDER_CLUSTER_DEPTH = int(os.getenv("ORDER_CLUSTER_DEPTH", "15"))
    ORDER_CLUSTER_RATIO = float(os.getenv("ORDER_CLUSTER_RATIO", "3.0"))

    # Перевес: окно (секунд) и порог доминирования
    DOMINANCE_SHIFT_WINDOW = int(os.getenv("DOMINANCE_SHIFT_WINDOW", "30"))
    DOMINANCE_SHIFT_THRESHOLD = float(os.getenv("DOMINANCE_SHIFT_THRESHOLD", "0.6"))

