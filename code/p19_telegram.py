"""
Мост между FluxerAi и Telegram-ботом пользователя.

Идея: на сайте выбирается модель/провайдер/системный промт (как для обычного
чата), пользователь вводит токен своего Telegram-бота (получен у @BotFather),
и дальше все сообщения в этом боте — текст/фото/видео/файл — идут напрямую
той же модели, а её ответ прилетает обратно в Telegram. Без команд — просто
чат с моделью через Telegram, как и попросили.

Подключение токена происходит двумя путями, ведущими к одному и тому же
эндпойнту /api/telegram/connect:
  1) Кнопка/поле в интерфейсе (раздел настроек Telegram).
  2) Через обычный чат: модель распознаёт "хочу общаться через бота" и вызывает
     call_tool: connect_telegram_bot — сервер в ответ на это шлёт SSE-событие
     ui_action, а фронтенд по нему сам открывает окно «введите токен».
"""

from flask import jsonify
from flask import request
import base64
import mimetypes
import threading
import time
import traceback
import requests

from p01_app import app
from p03_storage import _load_json, _save_json, _storage_lock
from config.settings import TELEGRAM_FILE, MAX_ATTACHMENT_BYTES
from p05_keys import get_key_sequence
from p09_settings_models import classify_mime
from p10_providers import PROVIDERS, ProviderError
from p13_routing import call_with_key_rotation, guess_provider_and_call
import p12_tools
from p12_tools import extract_tool_calls, run_tools

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

_DEFAULT_STATE = {
    "connected": False,
    "token": "",
    "provider": "",       # пусто = автоопределение по модели, как в обычном чате
    "model": "",
    "system_prompt": "",
    "bot_username": "",
}

_state = _load_json(TELEGRAM_FILE, dict(_DEFAULT_STATE))
for _k, _v in _DEFAULT_STATE.items():
    _state.setdefault(_k, _v)

# История переписки на каждый Telegram chat_id — отдельно от чатов на сайте,
# чтобы диалог в боте не путался и не тёр историю с веб-интерфейса.
_histories = {}
_histories_lock = threading.Lock()

_poll_thread = None
_poll_lock = threading.Lock()
_poll_stop_events = {}


def _persist_state():
    _save_json(TELEGRAM_FILE, _state)


def _tg_call(token, method, params=None, files=None, timeout=30):
    url = TELEGRAM_API.format(token=token, method=method)
    resp = requests.post(url, data=params or {}, files=files, timeout=timeout)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API {method}: {data.get('description', resp.text[:200])}")
    return data.get("result")


def _tg_send_text(token, chat_id, text):
    # Telegram режет сообщения по 4096 символов — режем длинные ответы на части.
    text = text or "(пустой ответ)"
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [text]
    for chunk in chunks:
        _tg_call(token, "sendMessage", {"chat_id": chat_id, "text": chunk})


def _tg_send_file(token, chat_id, name, mime_type, raw_bytes):
    kind = classify_mime(mime_type, name)
    method = {"image": "sendPhoto", "video": "sendVideo", "audio": "sendAudio"}.get(kind, "sendDocument")
    field = {"sendPhoto": "photo", "sendVideo": "video", "sendAudio": "audio"}.get(method, "document")
    files = {field: (name, raw_bytes, mime_type or "application/octet-stream")}
    _tg_call(token, method, {"chat_id": chat_id}, files=files)


def _tg_download_file(token, file_id):
    info = _tg_call(token, "getFile", {"file_id": file_id})
    file_path = info["file_path"]
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content, file_path


def _history_for(chat_id):
    with _histories_lock:
        return _histories.setdefault(chat_id, [])


def _append_history(chat_id, role, content):
    with _histories_lock:
        hist = _histories.setdefault(chat_id, [])
        hist.append({"role": role, "content": content})
        # ограничиваем историю, чтобы не раздувать контекст бесконечно
        if len(hist) > 60:
            del hist[: len(hist) - 60]


def _build_system_prompt():
    sp = (_state.get("system_prompt") or "").strip()
    # Инструменты (call_tool) в Telegram-режиме не нужны и не поддерживаются —
    # шлём только базовый системный промт пользователя/агента.
    return sp


def _run_model(chat_id, user_content):
    """user_content — либо строка, либо список частей (текст + описание вложения)."""
    _append_history(chat_id, "user", user_content)
    messages = list(_history_for(chat_id))

    provider = (_state.get("provider") or "").strip() or None
    model = (_state.get("model") or "").strip()
    system_prompt = _build_system_prompt()

    if not model:
        return "⚠️ Бот подключён, но на сайте не выбрана модель для Telegram. Открой сайт → раздел Telegram → выбери модель."

    try:
        if provider and provider in PROVIDERS:
            reply, _files = call_with_key_rotation(
                provider, model, messages, system_prompt, 1.0, 2048, [], False, False
            )
        else:
            reply, _files, _used = guess_provider_and_call(
                model, messages, system_prompt, 1.0, 2048, [],
                hinted_provider=provider, code_execution=False, web_search=False,
            )
    except ProviderError as e:
        return f"⚠️ Ошибка модели: {e}"
    except Exception as e:
        traceback.print_exc()
        return f"⚠️ Внутренняя ошибка: {e}"

    clean_reply, _tool_names = extract_tool_calls(reply)
    final_reply = (clean_reply or reply or "").strip()
    _append_history(chat_id, "assistant", final_reply)
    return final_reply


def _handle_text(token, chat_id, text):
    reply = _run_model(chat_id, text)
    _tg_send_text(token, chat_id, reply)


def _handle_media(token, chat_id, file_id, filename, mime_type, caption, kind_label):
    try:
        raw_bytes, file_path = _tg_download_file(token, file_id)
    except Exception as e:
        _tg_send_text(token, chat_id, f"⚠️ Не удалось скачать {kind_label} из Telegram: {e}")
        return

    if len(raw_bytes) > MAX_ATTACHMENT_BYTES:
        _tg_send_text(token, chat_id, f"⚠️ {kind_label.capitalize()} слишком большой(ое) — сервер принимает файлы до {MAX_ATTACHMENT_BYTES // (1024*1024)} МБ.")
        return

    name = filename or file_path.rsplit("/", 1)[-1]
    mime = mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"

    # Пока модель без вложений: сообщаем ей текстом, что пользователь прислал
    # файл такого-то типа/имени (плюс подпись, если есть) — большинство диалоговых
    # сценариев (какой это файл, что с ним сделать) это покрывает без полной
    # мультимодальной интеграции по каждому провайдеру.
    note = f"[Пользователь прислал {kind_label} «{name}» ({mime}, {len(raw_bytes)} байт) через Telegram.]"
    if caption:
        note += f" Подпись к файлу: {caption}"
    reply = _run_model(chat_id, note)
    _tg_send_text(token, chat_id, reply)


def _process_update(token, update):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]

    if "text" in msg:
        _handle_text(token, chat_id, msg["text"])
        return

    if "photo" in msg:
        largest = msg["photo"][-1]
        _handle_media(token, chat_id, largest["file_id"], "photo.jpg", "image/jpeg", msg.get("caption"), "фото")
        return

    if "video" in msg:
        v = msg["video"]
        _handle_media(token, chat_id, v["file_id"], v.get("file_name") or "video.mp4", v.get("mime_type") or "video/mp4", msg.get("caption"), "видео")
        return

    if "document" in msg:
        d = msg["document"]
        _handle_media(token, chat_id, d["file_id"], d.get("file_name") or "file", d.get("mime_type"), msg.get("caption"), "файл")
        return

    if "voice" in msg:
        v = msg["voice"]
        _handle_media(token, chat_id, v["file_id"], "voice.ogg", v.get("mime_type") or "audio/ogg", msg.get("caption"), "голосовое сообщение")
        return

    if "audio" in msg:
        a = msg["audio"]
        _handle_media(token, chat_id, a["file_id"], a.get("file_name") or "audio.mp3", a.get("mime_type") or "audio/mpeg", msg.get("caption"), "аудио")
        return

    # Прочие типы (стикеры, локации и т.п.) — тихо игнорируем, это не запрошено.


def _poll_loop(token, stop_event):
    offset = None
    while not stop_event.is_set():
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            updates = _tg_call(token, "getUpdates", params, timeout=35)
            for upd in updates:
                offset = upd["update_id"] + 1
                try:
                    _process_update(token, upd)
                except Exception:
                    traceback.print_exc()
        except requests.RequestException:
            time.sleep(3)
        except Exception:
            traceback.print_exc()
            time.sleep(3)


def _start_polling(token):
    global _poll_thread
    with _poll_lock:
        _stop_polling_locked()
        stop_event = threading.Event()
        _poll_stop_events[token] = stop_event
        t = threading.Thread(target=_poll_loop, args=(token, stop_event), daemon=True)
        _poll_thread = t
        t.start()


def _stop_polling_locked():
    global _poll_thread
    for ev in _poll_stop_events.values():
        ev.set()
    _poll_stop_events.clear()
    _poll_thread = None


def _stop_polling():
    with _poll_lock:
        _stop_polling_locked()


def _autostart_if_connected():
    if _state.get("connected") and _state.get("token"):
        try:
            _start_polling(_state["token"])
        except Exception:
            traceback.print_exc()


@app.route("/api/telegram/status", methods=["GET"])
def telegram_status():
    return jsonify({
        "connected": bool(_state.get("connected")),
        "bot_username": _state.get("bot_username", ""),
        "provider": _state.get("provider", ""),
        "model": _state.get("model", ""),
        "system_prompt": _state.get("system_prompt", ""),
    })


@app.route("/api/telegram/connect", methods=["POST"])
def telegram_connect():
    body = request.get_json(force=True) or {}
    token = (body.get("token") or "").strip()
    if not token:
        return jsonify({"error": "Токен пустой. Получи его у @BotFather в Telegram (команда /newbot) и вставь сюда."}), 400

    try:
        me = _tg_call(token, "getMe")
    except Exception as e:
        return jsonify({"error": f"Telegram не принял токен: {e}"}), 400

    with _storage_lock:
        _state["connected"] = True
        _state["token"] = token
        _state["bot_username"] = me.get("username", "")
        if "provider" in body:
            _state["provider"] = (body.get("provider") or "").strip()
        if "model" in body:
            _state["model"] = (body.get("model") or "").strip()
        if "system_prompt" in body:
            _state["system_prompt"] = body.get("system_prompt") or ""
    _persist_state()

    _start_polling(token)

    return jsonify({
        "ok": True,
        "bot_username": _state["bot_username"],
        "message": f"Готово! Бот @{_state['bot_username']} подключён — можно писать ему в Telegram.",
    })


@app.route("/api/telegram/settings", methods=["POST"])
def telegram_settings():
    """Смена модели/провайдера/промта для уже подключённого бота — без повторного ввода токена."""
    body = request.get_json(force=True) or {}
    with _storage_lock:
        for key in ("provider", "model", "system_prompt"):
            if key in body:
                _state[key] = body[key] or ""
    _persist_state()
    return jsonify({"ok": True})


@app.route("/api/telegram/disconnect", methods=["POST"])
def telegram_disconnect():
    with _storage_lock:
        _state["connected"] = False
        old_token = _state.get("token", "")
        _state["token"] = ""
        _state["bot_username"] = ""
    _persist_state()
    _stop_polling()
    with _histories_lock:
        _histories.clear()
    return jsonify({"ok": True})


_autostart_if_connected()
