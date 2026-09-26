"""
Мост между FluxerAi и Discord-ботом пользователя.

Та же идея, что и в p19_telegram.py: модель/провайдер/системный промт
настраиваются на сайте, пользователь один раз даёт токен своего Discord-бота
(создаётся на https://discord.com/applications), и дальше личные сообщения
(DM) этому боту — текст, картинки, видео, файлы — идут напрямую выбранной
модели, а её ответ прилетает обратно в Discord. Сообщения на серверах/каналах
бот не обрабатывает — только личка, как и попросили.

discord.py работает на asyncio, а весь остальной сервер — синхронный Flask,
поэтому клиент Discord живёт в отдельном потоке со своим собственным
event loop (тот же приём, каким работает Telegram-поток, только там простой
requests-polling, а тут asyncio-цикл discord.py).

discord.py — необязательная зависимость (см. requirements.txt): если её нет,
все /api/discord/* роуты остаются доступны, но connect вернёт понятную
ошибку вместо падения всего сервера.
"""

from flask import jsonify
from flask import request
import asyncio
import io
import threading
import traceback

from p01_app import app
from p03_storage import _load_json, _save_json, _storage_lock
from config.settings import DISCORD_FILE, MAX_ATTACHMENT_BYTES
from p09_settings_models import classify_mime
from p10_providers import PROVIDERS, ProviderError
from p13_routing import call_with_key_rotation, guess_provider_and_call
from p12_tools import extract_tool_calls

try:
    import discord
    DISCORD_PY_AVAILABLE = True
except ImportError:
    discord = None
    DISCORD_PY_AVAILABLE = False

_DEFAULT_STATE = {
    "connected": False,
    "token": "",
    "provider": "",       # пусто = автоопределение по модели, как в обычном чате
    "model": "",
    "system_prompt": "",
    "bot_username": "",
}

_state = _load_json(DISCORD_FILE, dict(_DEFAULT_STATE))
for _k, _v in _DEFAULT_STATE.items():
    _state.setdefault(_k, _v)

# История переписки на каждого Discord-пользователя (по user.id) — отдельно
# от чатов на сайте, чтобы диалог в боте не путался с веб-интерфейсом.
_histories = {}
_histories_lock = threading.Lock()

_client = None
_loop = None
_thread = None
_run_lock = threading.Lock()


def _persist_state():
    _save_json(DISCORD_FILE, _state)


def _history_for(user_id):
    with _histories_lock:
        return _histories.setdefault(user_id, [])


def _append_history(user_id, role, content):
    with _histories_lock:
        hist = _histories.setdefault(user_id, [])
        hist.append({"role": role, "content": content})
        if len(hist) > 60:
            del hist[: len(hist) - 60]


def _build_system_prompt():
    return (_state.get("system_prompt") or "").strip()


def _run_model(user_id, user_content):
    _append_history(user_id, "user", user_content)
    messages = list(_history_for(user_id))

    provider = (_state.get("provider") or "").strip() or None
    model = (_state.get("model") or "").strip()
    system_prompt = _build_system_prompt()

    if not model:
        return "⚠️ Бот подключён, но на сайте не выбрана модель для Discord. Открой сайт → раздел Discord → выбери модель."

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
    _append_history(user_id, "assistant", final_reply)
    return final_reply


def _kind_label(mime_type, filename):
    kind = classify_mime(mime_type, filename)
    return {"image": "фото", "video": "видео", "audio": "аудио"}.get(kind, "файл")


async def _send_long(channel, text):
    # Discord режет сообщения по 2000 символов.
    text = text or "(пустой ответ)"
    chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)] or [text]
    for chunk in chunks:
        await channel.send(chunk)


def _build_discord_client():
    intents = discord.Intents.default()
    intents.message_content = True
    intents.dm_messages = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        _state["bot_username"] = str(client.user)
        _persist_state()

    @client.event
    async def on_message(message):
        if message.author.bot:
            return
        # Только личные сообщения — на серверах/каналах бот молчит, как и попросили.
        if not isinstance(message.channel, discord.DMChannel):
            return

        user_id = str(message.author.id)

        # Вложения (фото/видео/файлы): скачиваем и сообщаем модели текстом,
        # что и какого размера прислали — как и в Telegram-мосте.
        if message.attachments:
            for att in message.attachments:
                if att.size and att.size > MAX_ATTACHMENT_BYTES:
                    await _send_long(message.channel, f"⚠️ Файл «{att.filename}» слишком большой — сервер принимает до {MAX_ATTACHMENT_BYTES // (1024*1024)} МБ.")
                    continue
                mime = att.content_type or ""
                label = _kind_label(mime, att.filename)
                note = f"[Пользователь прислал {label} «{att.filename}» ({mime or 'неизвестный тип'}, {att.size} байт) через Discord.]"
                if message.content:
                    note += f" Сообщение к файлу: {message.content}"
                reply = _run_model(user_id, note)
                await _send_long(message.channel, reply)
            return

        text = (message.content or "").strip()
        if not text:
            return
        reply = _run_model(user_id, text)
        await _send_long(message.channel, reply)

    return client


def _thread_main(token, loop):
    asyncio.set_event_loop(loop)
    global _client
    _client = _build_discord_client()
    try:
        loop.run_until_complete(_client.start(token))
    except Exception:
        traceback.print_exc()
    finally:
        try:
            loop.run_until_complete(_client.close())
        except Exception:
            pass


def _start_client(token):
    global _loop, _thread
    with _run_lock:
        _stop_client_locked()
        loop = asyncio.new_event_loop()
        _loop = loop
        t = threading.Thread(target=_thread_main, args=(token, loop), daemon=True)
        _thread = t
        t.start()


def _stop_client_locked():
    global _client, _loop, _thread
    if _client is not None and _loop is not None:
        try:
            asyncio.run_coroutine_threadsafe(_client.close(), _loop)
        except Exception:
            pass
    _client = None
    _loop = None
    _thread = None


def _stop_client():
    with _run_lock:
        _stop_client_locked()


def _autostart_if_connected():
    if DISCORD_PY_AVAILABLE and _state.get("connected") and _state.get("token"):
        try:
            _start_client(_state["token"])
        except Exception:
            traceback.print_exc()


@app.route("/api/discord/status", methods=["GET"])
def discord_status():
    return jsonify({
        "available": DISCORD_PY_AVAILABLE,
        "connected": bool(_state.get("connected")),
        "bot_username": _state.get("bot_username", ""),
        "provider": _state.get("provider", ""),
        "model": _state.get("model", ""),
        "system_prompt": _state.get("system_prompt", ""),
    })


@app.route("/api/discord/connect", methods=["POST"])
def discord_connect():
    if not DISCORD_PY_AVAILABLE:
        return jsonify({"error": "На сервере не установлена библиотека discord.py. Установи: pip install discord.py"}), 400

    body = request.get_json(force=True) or {}
    token = (body.get("token") or "").strip()
    if not token:
        return jsonify({"error": "Токен пустой. Создай бота на https://discord.com/developers/applications, вкладка Bot → Reset Token, и вставь токен сюда."}), 400

    with _storage_lock:
        _state["connected"] = True
        _state["token"] = token
        if "provider" in body:
            _state["provider"] = (body.get("provider") or "").strip()
        if "model" in body:
            _state["model"] = (body.get("model") or "").strip()
        if "system_prompt" in body:
            _state["system_prompt"] = body.get("system_prompt") or ""
    _persist_state()

    try:
        _start_client(token)
    except Exception as e:
        return jsonify({"error": f"Не удалось запустить Discord-клиент: {e}"}), 400

    return jsonify({
        "ok": True,
        "message": "Токен принят, бот запускается — проверь личные сообщения в Discord через пару секунд. Не забудь включить \"Message Content Intent\" в настройках бота на discord.com/developers, иначе бот не увидит текст сообщений.",
    })


@app.route("/api/discord/settings", methods=["POST"])
def discord_settings():
    """Смена модели/провайдера/промта для уже подключённого бота — без повторного ввода токена."""
    body = request.get_json(force=True) or {}
    with _storage_lock:
        for key in ("provider", "model", "system_prompt"):
            if key in body:
                _state[key] = body[key] or ""
    _persist_state()
    return jsonify({"ok": True})


@app.route("/api/discord/disconnect", methods=["POST"])
def discord_disconnect():
    with _storage_lock:
        _state["connected"] = False
        _state["token"] = ""
        _state["bot_username"] = ""
    _persist_state()
    _stop_client()
    with _histories_lock:
        _histories.clear()
    return jsonify({"ok": True})


_autostart_if_connected()
