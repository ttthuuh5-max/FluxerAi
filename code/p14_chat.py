from datetime import datetime
from datetime import timezone
from flask import jsonify
from flask import request
from flask import Response
import base64
import json
import re
import requests
import traceback
from p01_app import app
from p04_logs import RequestLogger
from p05_keys import get_key_sequence
from p09_settings_models import classify_mime
from p10_providers import PROVIDERS, ProviderError
from p11_media import (
    FAL_VIDEO_MODELS, LTX_VIDEO_MODELS, FISH_AUDIO_MODELS, IMAGE_MODELS,
    GenerationError, generate_image, generate_video, generate_audio,
)
import p12_tools
from p12_tools import MAX_TOOL_ROUNDS, extract_tool_calls, run_tools
from p13_routing import (
    call_with_key_rotation, guess_provider_and_call,
    call_with_key_rotation_stream, guess_provider_and_call_stream,
)
from p16_usage import start_collecting, stop_collecting, record_usage

from config.settings import MAX_ATTACHMENT_BYTES

def _normalize_attachments(raw_list):
    out = []
    if not isinstance(raw_list, list):
        return out
    for a in raw_list:
        if not isinstance(a, dict):
            continue
        name = (a.get("name") or "file").strip()
        mime_type = (a.get("mime_type") or "application/octet-stream").strip()
        data_b64 = a.get("data_base64") or ""
        if not data_b64:
            continue
        approx_bytes = len(data_b64) * 3 // 4
        if approx_bytes > MAX_ATTACHMENT_BYTES:
            continue
        out.append({
            "name": name,
            "mime_type": mime_type,
            "data_base64": data_b64,
            "kind": classify_mime(mime_type, name),
        })
    return out

SEND_FILE_RE = re.compile(
    r"send_file:\s*(?P<name>[^\n`]+?)\s*\n```(?:[a-zA-Z0-9_+-]*)\n(?P<body>.*?)```",
    re.DOTALL,
)

DEFAULT_SYSTEM_PROMPT_ADDON = (
    "\n\nДля файла используй ровно:\nsend_file: имя.расш\n```\nсодержимое\n```\n"
    "Без отдельных списков файлов, пояснение — одной строкой сразу после блока."
)

PLATFORM_CONTEXT_ADDON = (
    "\n\nТы работаешь в составе панели FluxerAi — локального AI-хаба, "
    "развёрнутого пользователем на его собственном сайте/сервере. Пользователь "
    "сам подключает провайдеров и модели через свои API-ключи."
)

def build_context_addon(client_time_iso=None, client_timezone=None, client_locale=None):
    now_utc = datetime.now(timezone.utc)
    parts = [f"UTC {now_utc.strftime('%Y-%m-%d %H:%M')}"]
    if client_time_iso:
        parts.append(f"локально {client_time_iso}")
    if client_timezone:
        parts.append(client_timezone)
    return "\n\nТекущее время: " + ", ".join(parts) + ". Считай это правдой."

WEB_SEARCH_DISABLED_ADDON = (
    "\n\nПоиск в интернете отключён — отвечай по своим знаниям, не притворяйся, что искал."
)

def _guess_mime_from_filename(name):
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
        "svg": "image/svg+xml", "webp": "image/webp",
        "pdf": "application/pdf",
        "json": "application/json", "csv": "text/csv", "txt": "text/plain",
        "md": "text/markdown", "html": "text/html", "css": "text/css",
        "js": "text/javascript", "py": "text/x-python", "java": "text/x-java-source",
        "c": "text/x-c", "cpp": "text/x-c++", "sh": "text/x-shellscript",
        "xml": "application/xml", "yaml": "text/yaml", "yml": "text/yaml",
        "zip": "application/zip",
    }.get(ext, "text/plain")

def extract_send_file_blocks(text, logger=None):
    if not text or "send_file:" not in text:
        return text, []

    files = []

    def _replace(match):
        name = match.group("name").strip()
        body = match.group("body")
        if body.endswith("\n"):
            body = body[:-1]
        data_b64 = base64.b64encode(body.encode("utf-8")).decode("ascii")
        mime = _guess_mime_from_filename(name or "file.txt")
        files.append({
            "name": name or "file.txt",
            "mime_type": mime,
            "data_base64": data_b64,
        })
        if logger:
            logger.log_action("send_file", {"name": name or "file.txt"}, f"файл готов: {name or 'file.txt'} ({mime}, {len(body)} симв.)")
        return f"📎 Файл «{name}» готов — смотри вложение ниже."

    clean_text = SEND_FILE_RE.sub(_replace, text)
    return clean_text.strip(), files

def _fetch_openai_image_models(key):
    resp = requests.get(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {key}"},
        timeout=20,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, f"OpenAI /v1/models {resp.status_code}: {resp.text[:200]}")
    ids = [m["id"] for m in resp.json().get("data", [])]
    image_ids = sorted(m for m in ids if m.startswith("dall-e") or m.startswith("gpt-image"))
    return image_ids or IMAGE_MODELS.get("openai", [])

def _fetch_google_image_models(key):
    resp = requests.get(
        f"https://generativelanguage.googleapis.com/v1beta/models?key={key}",
        timeout=20,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, f"Google /v1beta/models {resp.status_code}: {resp.text[:200]}")
    models = resp.json().get("models", [])
    image_ids = []
    for m in models:
        name = m.get("name", "").split("/")[-1]
        methods = m.get("supportedGenerationMethods", [])
        if "generateContent" in methods and ("image" in name.lower()):
            image_ids.append(name)
    return sorted(set(image_ids)) or IMAGE_MODELS.get("google", [])

def _fetch_fish_audio_models(key):
    resp = requests.get(
        "https://api.fish.audio/v1/model",
        headers={"Authorization": f"Bearer {key}"},
        timeout=20,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, f"Fish Audio /v1/model {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    items = data.get("items", data if isinstance(data, list) else [])
    ids = [it.get("_id") or it.get("id") for it in items if isinstance(it, dict)]
    ids = [i for i in ids if i]
    return sorted(set(ids)) or FISH_AUDIO_MODELS

def _fetch_pollinations_image_models(key):
    resp = requests.get(
        "https://gen.pollinations.ai/image/models",
        headers={"Authorization": f"Bearer {key}"},
        timeout=20,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, f"Pollinations /image/models {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    items = data if isinstance(data, list) else data.get("models", data.get("data", []))
    image_ids = []
    for m in items:
        if not isinstance(m, dict):
            continue
        model_id = m.get("id") or m.get("name")
        if not model_id:
            continue
        outputs = m.get("outputModalities") or m.get("output_modalities") or []

        if not outputs or "image" in outputs:
            image_ids.append(model_id)
    return sorted(set(image_ids)) or IMAGE_MODELS.get("pollinations", [])

def _live_models_or_fallback(provider, fetch_fn, fallback):
    keys = get_key_sequence(provider)
    if not keys:
        return fallback
    try:
        return fetch_fn(keys[0])
    except Exception:
        traceback.print_exc()
        return fallback

@app.route("/api/media_models", methods=["GET"])
def media_models_list():
    return jsonify({
        "image": {
            "google": _live_models_or_fallback("google", _fetch_google_image_models, IMAGE_MODELS.get("google", [])),
            "openai": _live_models_or_fallback("openai", _fetch_openai_image_models, IMAGE_MODELS.get("openai", [])),
            "cloudflare": IMAGE_MODELS.get("cloudflare", []),
            "pollinations": _live_models_or_fallback("pollinations", _fetch_pollinations_image_models, IMAGE_MODELS.get("pollinations", [])),
        },
        "video": {
            "fal": FAL_VIDEO_MODELS,
            "ltx": LTX_VIDEO_MODELS,
        },
        "audio": {
            "fish_audio": _live_models_or_fallback("fish_audio", _fetch_fish_audio_models, FISH_AUDIO_MODELS),
        },
    })

@app.route("/api/generate", methods=["POST"])
def generate_media():
    """Прямая генерация фото/видео/аудио для вкладки-редактора (не через чат)."""
    body = request.get_json(force=True) or {}
    kind = (body.get("kind") or "").strip().lower()
    prompt = (body.get("prompt") or "").strip()
    provider = (body.get("provider") or "").strip().lower() or None
    model = (body.get("model") or "").strip() or None

    if kind not in ("image", "video", "audio"):
        return jsonify({"error": "Не указан тип генерации (kind: image / video / audio)"}), 400
    if not prompt:
        return jsonify({"error": "Не указан текст запроса"}), 400

    try:
        if kind == "image":
            file_info = generate_image(prompt, model=model, provider=provider)
        elif kind == "video":
            duration = body.get("duration")
            try:
                duration = int(duration) if duration else None
            except (TypeError, ValueError):
                duration = None
            file_info = generate_video(prompt, model=model, provider=provider, duration=duration)
        else:
            voice_id = (body.get("voice_id") or "").strip() or None
            file_info = generate_audio(prompt, model=model, voice_id=voice_id)
        return jsonify({"file": file_info})
    except GenerationError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

TITLE_SYSTEM_PROMPT = (
    "Название чата: 2-4 слова, рус., без кавычек/точки/markdown. Ответь только названием."
)

@app.route("/api/title_chat", methods=["POST"])
def title_chat():
    body = request.get_json(force=True) or {}
    model = body.get("model")
    provider = (body.get("provider") or "").strip().lower() or None
    if provider == "auto":
        provider = None
    user_message = (body.get("user_message") or "").strip()
    assistant_message = (body.get("assistant_message") or "").strip()

    if not user_message:
        return jsonify({"error": "Не передано user_message"}), 400

    convo = f"Сообщение пользователя: {user_message[:500]}"
    if assistant_message:
        convo += f"\n\nОтвет ассистента: {assistant_message[:500]}"

    try:
        reply, _files, _used = guess_provider_and_call(
            model,
            [{"role": "user", "content": convo}],
            TITLE_SYSTEM_PROMPT,
            0.5,
            20,
            [],
            hinted_provider=provider,
        )
        title = reply.strip().strip('"').strip("'").strip(".")

        if len(title) > 60:
            title = title[:60].rstrip()
        if not title:
            title = "Новый чат"
        return jsonify({"title": title})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e), "title": None}), 500

def _sse(event_type, data):
    return f"data: {json.dumps({'type': event_type, **data}, ensure_ascii=False)}\n\n"

def _run_stream_round(call_stream_fn, current_messages):
    """Стримит один раунд генерации. yield-ит SSE-строки с дельтами текста
    клиенту сразу же; параллельно копит полный текст/usage/файлы для логики
    инструментов и логирования. В конце yield-ит специальный маркер-словарь
    (не строку) с итогами раунда — вызывающий код должен отличать str от dict."""
    full_text_parts = []
    files = []
    usage_raw = None
    usage_style = "openai"
    used_provider = None
    for kind, val in call_stream_fn(current_messages):
        if kind == "text":
            full_text_parts.append(val)
            yield _sse("delta", {"text": val})
        elif kind == "usage_typed":
            usage_raw, usage_style = val
        elif kind == "usage":
            usage_raw, usage_style = val, "anthropic"
        elif kind == "files":
            files = val
        elif kind == "provider":
            used_provider = val
    if usage_raw:
        record_usage(usage_raw, usage_style)
    yield {"__round_done__": True, "text": "".join(full_text_parts), "files": files, "provider": used_provider}

@app.route("/api/chat", methods=["POST"])
def chat():
    collector, token = start_collecting()

    def generate():
        try:
            for chunk in _chat_impl_stream(collector):
                yield chunk
        finally:
            stop_collecting(token)

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })

def _chat_impl_stream(usage_collector):
    body = request.get_json(force=True) or {}

    model = body.get("model")
    provider = (body.get("provider") or "").strip().lower() or None
    if provider == "auto":
        provider = None
    messages = body.get("messages", [])

    system_prompt = body.get("system_prompt", "") or ""

    # Кнопка «Не применять системные промты»: если включена — модель (любая, не только
    # локальная) получает только историю диалога, без единого системного сообщения
    # (ни пользовательского, ни служебных добавок сервера).
    skip_system_prompts = bool(body.get("disable_system_prompts_for_local", False))

    if skip_system_prompts:
        system_prompt = ""
    else:
        if not body.get("disable_context_addon", False):
            system_prompt = (system_prompt + build_context_addon(
                client_time_iso=body.get("client_time"),
                client_timezone=body.get("client_timezone"),
                client_locale=body.get("client_locale"),
            )).strip()

        if not body.get("disable_send_file", False):
            system_prompt = (system_prompt + DEFAULT_SYSTEM_PROMPT_ADDON).strip()

        system_prompt = (system_prompt + PLATFORM_CONTEXT_ADDON).strip()

    # Генерация фото/видео/аудио больше не встроена в обычный чат — для неё есть
    # отдельная вкладка-редактор (/api/generate). Модель в чате медиа не генерирует.

    # Без системного промта модель не знает про формат вызова инструментов, поэтому
    # при пропуске промтов инструменты тоже отключаем (иначе парсер зря искал бы вызовы).
    tools_enabled = (not body.get("disable_tools", False)) and not skip_system_prompts
    if tools_enabled:
        full_tools_prompt = bool(body.get("full_tools_prompt", False))
        tools_addon = (
            p12_tools.TOOLS_SYSTEM_PROMPT_ADDON_FULL
            if full_tools_prompt else p12_tools.TOOLS_SYSTEM_PROMPT_ADDON
        )
        system_prompt = (system_prompt + tools_addon).strip()

    temperature = float(body.get("temperature", 1.0))
    max_tokens = int(body.get("max_tokens", 4096))
    attachments = _normalize_attachments(body.get("attachments", []))

    code_execution = bool(body.get("code_execution", False))

    web_search = bool(body.get("web_search", False))
    if not web_search and not skip_system_prompts:
        system_prompt = (system_prompt + WEB_SEARCH_DISABLED_ADDON).strip()

    if not model:
        return jsonify({"error": "Не указана модель"}), 400

    logger = RequestLogger(provider, model, system_prompt, messages, attachments)
    fixed_provider = bool(provider and provider in PROVIDERS)

    def _make_round_stream(current_messages):
        if fixed_provider:
            return call_with_key_rotation_stream(
                provider, model, current_messages, system_prompt, temperature, max_tokens, attachments, code_execution, web_search
            )
        return guess_provider_and_call_stream(
            model, current_messages, system_prompt, temperature, max_tokens, attachments,
            hinted_provider=provider, code_execution=code_execution, web_search=web_search,
        )

    def _run_stream_with_tools():
        """Генератор SSE-строк. yield-ит только строки 'data: ...\\n\\n';
        по завершении всех раундов инструментов отправляет событие 'final'
        с итоговым (после send_file/файлов) текстом и метаданными."""
        current_messages = messages
        used_provider = provider
        auto_detected = not fixed_provider
        all_files = []
        reply = ""
        tool_rounds_exceeded = False

        for _round in range(MAX_TOOL_ROUNDS):
            round_result = None
            for chunk in _run_stream_round(_make_round_stream, current_messages):
                if isinstance(chunk, dict) and chunk.get("__round_done__"):
                    round_result = chunk
                else:
                    yield chunk
            reply = round_result["text"]
            all_files = all_files + round_result["files"]
            if round_result["provider"]:
                used_provider = round_result["provider"]

            if not tools_enabled:
                logger.log_round(reply, reply, [], "")
                break

            clean_reply, tool_names = extract_tool_calls(reply)
            if not tool_names:
                logger.log_round(reply, clean_reply, [], "")
                break

            # Инструменты выполняются быстро и не стримятся — но клиент должен
            # знать, что генерация продолжается (следующий раунд начнётся сразу).
            tool_results_text, tool_files = run_tools(tool_names, request, body)
            if tool_files:
                all_files = all_files + tool_files
            logger.log_round(reply, clean_reply, tool_names, tool_results_text)
            yield _sse("tool_round", {"tool_calls": [n for n, _ in tool_names]})
            current_messages = current_messages + [
                {"role": "assistant", "content": clean_reply or reply},
                {"role": "user", "content": tool_results_text},
            ]
        else:
            clean_reply, _ = extract_tool_calls(reply or "")
            final_reply = (clean_reply or reply or "").strip()
            note = "\n\n[Ответ прерван: превышен лимит обращений к инструментам за один запрос.]"
            final_reply = (final_reply + note) if final_reply else note.strip()
            logger.log_action("tool_rounds_exceeded", args={"max_rounds": MAX_TOOL_ROUNDS}, result="лимит раундов call_tool исчерпан")
            reply = final_reply
            tool_rounds_exceeded = True

        # Если реплика пришла после раунда с инструментами, tools_enabled=True,
        # clean_reply уже вычислен выше в цикле — но если цикл вышел через break
        # без call_tool, reply уже "чистый" (без call_tool: строк).
        if tools_enabled:
            clean_final, _ = extract_tool_calls(reply)
            reply = clean_final or reply

        reply, extra_files = extract_send_file_blocks(reply, logger=logger)
        all_files = all_files + extra_files + body.get("_video_output_files", [])

        usage = usage_collector.to_dict()
        final_payload = {
            "reply": reply,
            "provider": used_provider,
            "model": model,
            "files": all_files,
            "auto_detected": auto_detected,
        }
        if usage:
            final_payload["usage"] = usage
        if tool_rounds_exceeded:
            final_payload["tool_rounds_exceeded"] = True

        logger.finish("ok", final_reply=reply, used_provider=used_provider, auto_detected=auto_detected, usage=usage)
        yield _sse("final", final_payload)

    try:
        for chunk in _run_stream_with_tools():
            yield chunk
    except ProviderError as e:
        status = e.status_code if e.status_code >= 400 else 500
        logger.finish("error", error=str(e), used_provider=provider, auto_detected=not fixed_provider, usage=usage_collector.to_dict())
        yield _sse("error", {"error": str(e), "status_code": status, "is_limit": status == 429})
    except Exception as e:
        traceback.print_exc()
        logger.finish("error", error=str(e), used_provider=provider, auto_detected=not fixed_provider, usage=usage_collector.to_dict())
        yield _sse("error", {"error": str(e), "status_code": 500})
