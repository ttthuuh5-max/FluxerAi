import atexit
import threading
from datetime import datetime
from datetime import timezone
from flask import jsonify
from flask import request
from p01_app import app
from p03_storage import CHATS_FILE, _load_json, _save_json, _storage_lock

_chats_store = _load_json(CHATS_FILE, {"chats": []})
if not isinstance(_chats_store.get("chats"), list):
    _chats_store["chats"] = []

# Запись chats.json на диск — «с задержкой»: при потоковом общении чат сохраняется после каждого
# сообщения, а файл растёт с историей. Раньше каждое сохранение = полная перезапись файла прямо
# в запросе. Теперь несколько сохранений подряд склеиваются в одну запись в фоне.
_PERSIST_DELAY = 0.8
_persist_timer = None
_persist_guard = threading.Lock()

def _flush_chats():
    global _persist_timer
    with _persist_guard:
        _persist_timer = None
    with _storage_lock:
        # снимок под блокировкой, а на диск пишем уже без неё
        snapshot = {"chats": list(_chats_store["chats"])}
    _save_json(CHATS_FILE, snapshot)

def _persist_chats():
    global _persist_timer
    with _persist_guard:
        if _persist_timer is not None:
            return  # запись уже запланирована — она подхватит и это изменение
        _persist_timer = threading.Timer(_PERSIST_DELAY, _flush_chats)
        _persist_timer.daemon = True
        _persist_timer.start()

def _flush_now():
    """Дозаписать отложенное при выходе, чтобы не потерять последние сообщения."""
    with _persist_guard:
        pending = _persist_timer
    if pending is not None:
        pending.cancel()
        _flush_chats()

atexit.register(_flush_now)

@app.route("/api/chats", methods=["GET"])
def list_chats():
    return jsonify({"chats": _chats_store["chats"]})

@app.route("/api/chats", methods=["POST"])
def upsert_chat():
    body = request.get_json(force=True) or {}
    chat_id = body.get("id")
    if not chat_id:
        return jsonify({"error": "Не передан id чата"}), 400

    chat = {
        "id": chat_id,
        "title": body.get("title") or "Новый чат",
        "history": body.get("history") or [],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    with _storage_lock:
        chats = _chats_store["chats"]
        idx = next((i for i, c in enumerate(chats) if c.get("id") == chat_id), None)
        if idx is None:
            chats.insert(0, chat)
        else:
            chats[idx] = chat
    _persist_chats()
    return jsonify({"ok": True, "chat": chat})

@app.route("/api/chats/<chat_id>", methods=["DELETE"])
def delete_chat(chat_id):
    with _storage_lock:
        before = len(_chats_store["chats"])
        _chats_store["chats"] = [c for c in _chats_store["chats"] if c.get("id") != chat_id]
        removed = before - len(_chats_store["chats"])
    _persist_chats()
    return jsonify({"ok": True, "removed": removed})

@app.route("/api/chats", methods=["DELETE"])
def delete_all_chats():
    with _storage_lock:
        _chats_store["chats"] = []
    _persist_chats()
    return jsonify({"ok": True})
