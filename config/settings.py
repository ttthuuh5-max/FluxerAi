import os

APP_VERSION = "1.7.0"

CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR   = os.path.dirname(CONFIG_DIR)
CODE_DIR   = os.path.join(BASE_DIR, "code")
# FLUXERAI_DATA_DIR — куда складывать ключи, чаты, промты и логи (одиночный main.py ставит его сам).
DATA_DIR   = os.path.abspath(os.environ.get("FLUXERAI_DATA_DIR") or os.path.join(CONFIG_DIR, "data"))

KEYS_FILE          = os.path.join(DATA_DIR, "keys.json")
CHATS_FILE         = os.path.join(DATA_DIR, "chats.json")
PROMPTS_FILE       = os.path.join(DATA_DIR, "prompts.json")
STORE_PROMPTS_FILE = os.path.join(DATA_DIR, "store_prompts.json")
SETTINGS_FILE      = os.path.join(DATA_DIR, "settings.json")
AGENTS_FILE        = os.path.join(DATA_DIR, "agents.json")
TELEGRAM_FILE      = os.path.join(DATA_DIR, "telegram.json")
DISCORD_FILE       = os.path.join(DATA_DIR, "discord.json")
LOGS_DB_FILE       = os.path.join(DATA_DIR, "logs.db")

LOCAL_MODELS_DIR       = os.path.join(DATA_DIR, "local_models")
LOCAL_MODELS_META_FILE = os.path.join(DATA_DIR, "local_models.json")

FRONTEND_FILE = os.path.join(CONFIG_DIR, "frontend.html")
SPLASH_FILE   = os.path.join(CONFIG_DIR, "splash.html")

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 5000))
DEBUG = os.environ.get("DEBUG", "1") not in ("0", "false", "False")
OPEN_BROWSER = os.environ.get("OPEN_BROWSER", "1") not in ("0", "false", "False")

# Заставка при запуске (молния -> текст -> статус -> 3 2 1). SPLASH=0 — отключить.
# SPLASH_WAIT — сколько секунд ждать, пока браузер откроет страницу (если не открыл — стартуем без заставки).
SPLASH = os.environ.get("SPLASH", "1") not in ("0", "false", "False")
SPLASH_WAIT = float(os.environ.get("SPLASH_WAIT", 15))

MAX_AGENTS = 8
MAX_ATTACHMENT_BYTES = 40 * 1024 * 1024
MAX_TOOL_ROUNDS = 3
MODEL_LIST_CACHE_TTL = 300
