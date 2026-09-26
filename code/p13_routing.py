from flask import jsonify
from flask import request
import requests
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from p01_app import app
from p05_keys import API_KEYS, get_key_sequence
from p09_settings_models import KNOWN_MODELS, PROVIDER_ATTACHMENT_SUPPORT, is_thinking_model
from p10_providers import PROVIDERS, STREAMING_PROVIDERS, ProviderError, RETRYABLE_STATUS, OPENAI_COMPAT_ENDPOINTS, OPENAI_COMPAT_MODELS_URLS

def call_with_key_rotation_stream(provider, model, messages, system_prompt, temperature, max_tokens, attachments, code_execution=False, web_search=False):
    """Как call_with_key_rotation, но генератор: yield-ит ('text', str), ('usage_typed', (raw, style))
    или ('files', list) по мере поступления. Провайдеры без потоковой реализации
    (в STREAMING_PROVIDERS их нет) вызываются как раньше и результат отдаётся одним чанком."""
    stream_fn = STREAMING_PROVIDERS.get(provider)
    plain_fn = PROVIDERS.get(provider)
    if plain_fn is None:
        raise ProviderError(400, f"Неизвестный провайдер '{provider}' (нет реализации call_{provider})")

    if provider == "local":
        if stream_fn is not None:
            yield from stream_fn(model, messages, system_prompt, temperature, max_tokens, None, attachments, code_execution, web_search)
        else:
            text, files = plain_fn(model, messages, system_prompt, temperature, max_tokens, None, attachments, code_execution, web_search)
            yield ("text", text)
            yield ("files", files)
        return

    keys = get_key_sequence(provider)
    if not keys:
        raise ProviderError(
            401,
            f"Нет ни одного ключа для '{provider}'. Добавь ключ(и) в правой панели "
            f"(поле «{provider}») и нажми «Сохранить ключи на сервере».",
        )

    last_error = None
    for i, key in enumerate(keys):
        try:
            if stream_fn is not None:
                yield from stream_fn(model, messages, system_prompt, temperature, max_tokens, key, attachments, code_execution, web_search)
            else:
                text, files = plain_fn(model, messages, system_prompt, temperature, max_tokens, key, attachments, code_execution, web_search)
                yield ("text", text)
                yield ("files", files)
            return
        except ProviderError as e:
            last_error = e
            if e.status_code in RETRYABLE_STATUS and i < len(keys) - 1:
                continue
            raise
        except requests.RequestException as e:
            last_error = ProviderError(0, f"Сетевая ошибка: {e}")
            if i < len(keys) - 1:
                continue
            raise last_error

    if last_error:
        raise last_error
    raise ProviderError(500, "Все ключи провайдера не сработали")

def call_with_key_rotation(provider, model, messages, system_prompt, temperature, max_tokens, attachments, code_execution=False, web_search=False):
    fn = PROVIDERS.get(provider)
    if fn is None:
        raise ProviderError(400, f"Неизвестный провайдер '{provider}' (нет реализации call_{provider})")

    if provider == "local":

        return fn(model, messages, system_prompt, temperature, max_tokens, None, attachments, code_execution, web_search)

    keys = get_key_sequence(provider)
    if not keys:
        raise ProviderError(
            401,
            f"Нет ни одного ключа для '{provider}'. Добавь ключ(и) в правой панели "
            f"(поле «{provider}») и нажми «Сохранить ключи на сервере».",
        )

    last_error = None
    for i, key in enumerate(keys):
        try:
            return fn(model, messages, system_prompt, temperature, max_tokens, key, attachments, code_execution, web_search)
        except ProviderError as e:
            last_error = e
            if e.status_code in RETRYABLE_STATUS and i < len(keys) - 1:
                continue
            raise
        except requests.RequestException as e:
            last_error = ProviderError(0, f"Сетевая ошибка: {e}")
            if i < len(keys) - 1:
                continue
            raise last_error

    if last_error:
        raise last_error
    raise ProviderError(500, "Все ключи провайдера не сработали")

def model_exists_at(provider, model_name):
    known = KNOWN_MODELS.get(provider, [])
    if model_name in known or any(model_name in k or k in model_name for k in known):
        return True

    names = fetch_live_models(provider)
    return model_name in names

@app.route("/api/find_model", methods=["POST"])
def find_model():
    body = request.get_json(force=True) or {}
    model_name = (body.get("model_name") or "").strip()

    if not model_name:
        return jsonify({"error": "model_name пустой"}), 400

    results = []
    best = None
    for provider in PROVIDERS.keys():
        has_key = bool(get_key_sequence(provider))
        found = has_key and model_exists_at(provider, model_name)
        results.append({"provider": provider, "found": found, "has_key": has_key})
        if found and best is None:
            best = {"name": model_name, "provider": provider}

    return jsonify({"results": results, "best": best})

def _candidate_providers(model, hinted_provider):
    candidates = []
    if hinted_provider and hinted_provider in PROVIDERS:
        candidates.append(hinted_provider)
    for p, names in KNOWN_MODELS.items():
        if p not in candidates and model in names:
            candidates.append(p)
    for p, names in KNOWN_MODELS.items():
        if p not in candidates and any(model in n or n in model for n in names):
            candidates.append(p)
    for p in PROVIDERS:
        if p not in candidates and get_key_sequence(p) and model_exists_at(p, model):
            candidates.append(p)
    for p in PROVIDERS:
        if p not in candidates and get_key_sequence(p):
            candidates.append(p)
    return candidates

def guess_provider_and_call_stream(model, messages, system_prompt, temperature, max_tokens, attachments, hinted_provider=None, code_execution=False, web_search=False):
    """Как guess_provider_and_call, но генератор. Первый чанк текста, который
    успешно пришёл, фиксирует выбранного провайдера — до этого при ошибке
    молча пробуем следующего кандидата (как и в блокирующей версии)."""
    candidates = _candidate_providers(model, hinted_provider)
    tried = []
    last_error = None
    for provider in candidates:
        tried.append(provider)
        try:
            started = False
            for kind, val in call_with_key_rotation_stream(provider, model, messages, system_prompt, temperature, max_tokens, attachments, code_execution, web_search):
                if not started:
                    started = True
                    yield ("provider", provider)
                yield (kind, val)
            return
        except ProviderError as e:
            last_error = e
            continue

    if last_error:
        raise ProviderError(
            last_error.status_code,
            f"Не удалось подобрать провайдера для модели '{model}' (пробовал: {', '.join(tried) or 'никого — нет ключей'}). "
            f"Последняя ошибка: {last_error}",
        )
    raise ProviderError(
        400,
        f"Не удалось подобрать провайдера для модели '{model}'. Нет ни одного провайдера с ключом, "
        f"который стоило бы попробовать. Добавь ключи в правой панели.",
    )

def guess_provider_and_call(model, messages, system_prompt, temperature, max_tokens, attachments, hinted_provider=None, code_execution=False, web_search=False):
    tried = []
    candidates = []

    if hinted_provider and hinted_provider in PROVIDERS:
        candidates.append(hinted_provider)

    for p, names in KNOWN_MODELS.items():
        if p not in candidates and model in names:
            candidates.append(p)

    for p, names in KNOWN_MODELS.items():
        if p not in candidates and any(model in n or n in model for n in names):
            candidates.append(p)

    for p in PROVIDERS:
        if p not in candidates and get_key_sequence(p) and model_exists_at(p, model):
            candidates.append(p)

    for p in PROVIDERS:
        if p not in candidates and get_key_sequence(p):
            candidates.append(p)

    last_error = None
    for provider in candidates:
        tried.append(provider)
        try:
            reply, files = call_with_key_rotation(provider, model, messages, system_prompt, temperature, max_tokens, attachments, code_execution, web_search)
            return reply, files, provider
        except ProviderError as e:
            last_error = e
            continue

    if last_error:
        raise ProviderError(
            last_error.status_code,
            f"Не удалось подобрать провайдера для модели '{model}' (пробовал: {', '.join(tried) or 'никого — нет ключей'}). "
            f"Последняя ошибка: {last_error}",
        )
    raise ProviderError(
        400,
        f"Не удалось подобрать провайдера для модели '{model}'. Нет ни одного провайдера с ключом, "
        f"который стоило бы попробовать. Добавь ключи в правой панели.",
    )

_model_list_cache = {}
from config.settings import MODEL_LIST_CACHE_TTL

def fetch_live_models(provider, only_key=None):
    """Список моделей провайдера. only_key — спросить именно этим ключом (без кэша),
    иначе берётся первый ключ из пула и используется кэш."""
    if only_key is None:
        cached = _model_list_cache.get(provider)
        if cached and (time.time() - cached["ts"]) < MODEL_LIST_CACHE_TTL:
            return cached["models"]
        keys = get_key_sequence(provider)
    else:
        keys = [only_key]
    if not keys:
        return []

    names = []
    try:
        if provider == "google":
            resp = requests.get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={keys[0]}&pageSize=1000",
                timeout=15,
            )
            if resp.status_code == 200:
                for m in resp.json().get("models", []):
                    methods = m.get("supportedGenerationMethods", [])

                    if "generateContent" in methods:
                        names.append(m["name"].split("/")[-1])

        elif provider == "openai":
            resp = requests.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {keys[0]}"}, timeout=15,
            )
            if resp.status_code == 200:
                names = [m["id"] for m in resp.json().get("data", [])]

        elif provider == "openrouter":
            resp = requests.get("https://openrouter.ai/api/v1/models", timeout=15)
            if resp.status_code == 200:
                names = [m["id"] for m in resp.json().get("data", [])]

        elif provider == "mistral":
            resp = requests.get(
                "https://api.mistral.ai/v1/models",
                headers={"Authorization": f"Bearer {keys[0]}"}, timeout=15,
            )
            if resp.status_code == 200:
                names = [m["id"] for m in resp.json().get("data", [])]

        elif provider == "xai":
            resp = requests.get(
                "https://api.x.ai/v1/models",
                headers={"Authorization": f"Bearer {keys[0]}"}, timeout=15,
            )
            if resp.status_code == 200:
                names = [m["id"] for m in resp.json().get("data", [])]

        elif provider == "deepseek":
            resp = requests.get(
                "https://api.deepseek.com/models",
                headers={"Authorization": f"Bearer {keys[0]}"}, timeout=15,
            )
            if resp.status_code == 200:
                names = [m["id"] for m in resp.json().get("data", [])]

        elif provider == "anthropic":
            resp = requests.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": keys[0], "anthropic-version": "2023-06-01"}, timeout=15,
            )
            if resp.status_code == 200:
                names = [m["id"] for m in resp.json().get("data", [])]

        elif provider in OPENAI_COMPAT_ENDPOINTS:

            models_url = OPENAI_COMPAT_MODELS_URLS.get(provider) or (
                OPENAI_COMPAT_ENDPOINTS[provider][1].rsplit("/chat/completions", 1)[0] + "/models"
            )
            resp = requests.get(
                models_url,
                headers={"Authorization": f"Bearer {keys[0]}"}, timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()

                items = data.get("data", []) if isinstance(data, dict) else data
                names = [m["id"] for m in items if isinstance(m, dict) and m.get("id")]

    except requests.RequestException:
        names = []

    if names and only_key is None:
        _model_list_cache[provider] = {"ts": time.time(), "models": names}
    return names

# ---------------------------------------------------------------------------
# Проверка моделей отключена: ключи больше не прогоняются пробными запросами
# по каждой модели провайдера. _checked_status теперь всегда пустой — статусы
# "ok"/"error" по моделям в интерфейсе больше не проставляются.
# ---------------------------------------------------------------------------

def _checked_status(provider):
    return {}

@app.route("/api/model_status", methods=["GET"])
def model_status():
    """Что уже проверено по ключам, лежащим на сервере: {провайдер: {модель: {ok, error}}}."""
    out = {}
    for provider in PROVIDERS.keys():
        merged = _checked_status(provider)
        if merged:
            out[provider] = merged
    resp = jsonify({"status": out})
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.route("/api/version", methods=["GET"])
def version():
    from config.settings import APP_VERSION
    return jsonify({"version": APP_VERSION})

@app.route("/api/health", methods=["GET"])
def health():
    models = []
    for provider in PROVIDERS.keys():
        if provider == "local":
            continue
        if not get_key_sequence(provider):
            continue
        live = fetch_live_models(provider)
        names = live if live else KNOWN_MODELS.get(provider, [])
        supported_kinds = sorted(PROVIDER_ATTACHMENT_SUPPORT.get(provider, set()))
        checked = _checked_status(provider)
        for name in names:
            entry = {
                "name": name,
                "provider": provider,

                "supports": supported_kinds,

                "thinking": is_thinking_model(provider, name),
            }
            st = checked.get(name)
            if st is not None:          # None = ещё не проверялась → интерфейс покажет обычный цвет
                entry["status"] = "ok" if st["ok"] else "error"
                if not st["ok"]:
                    entry["status_error"] = st["error"]
            models.append(entry)

    try:
        from p17_local_models import list_installed_model_names
        for name in list_installed_model_names():
            models.append({"name": name, "provider": "local", "supports": [], "thinking": False})
    except Exception:
        pass

    key_counts = {p: len(API_KEYS.get(p, [])) for p in API_KEYS}
    return jsonify({"status": "ok", "models": models, "key_counts": key_counts})
