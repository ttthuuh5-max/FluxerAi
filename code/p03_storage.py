import json
import os
import threading
import traceback

from config.settings import (
    BASE_DIR, DATA_DIR, KEYS_FILE, CHATS_FILE, PROMPTS_FILE,
    STORE_PROMPTS_FILE, SETTINGS_FILE,
)

os.makedirs(DATA_DIR, exist_ok=True)

# Общий lock для мутаций in-memory хранилищ (_chats_store, _agents_store и т.д.).
# Импортируется из p06/p07/p08/p09/p17. Запись на диск блокируется отдельно
# через _lock_for(path) внутри _save_json.
_storage_lock = threading.RLock()

_storage_locks = {}
_storage_locks_guard = threading.Lock()

def _lock_for(path):
    with _storage_locks_guard:
        lock = _storage_locks.get(path)
        if lock is None:
            lock = threading.Lock()
            _storage_locks[path] = lock
        return lock

def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        traceback.print_exc()
        return default

def _save_json(path, data):
    tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with _lock_for(path):
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
