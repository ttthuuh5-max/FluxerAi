from flask import jsonify
from flask import request
import itertools
import os
import threading
from urllib.parse import urlparse
from p01_app import app
from p03_storage import KEYS_FILE, _load_json, _save_json

_lock = threading.Lock()

_DEFAULT_KEY_PROVIDERS = [
    "google", "openai", "anthropic", "openrouter", "mistral",
    "xai", "deepseek", "grokified", "groq", "perplexity", "together",
    "cohere", "fal", "fish_audio", "pollinations", "cloudflare",

    "cerebras", "sambanova", "fireworks", "nvidia", "hyperbolic",
    "deepinfra", "novita", "nebius", "moonshot", "zai",
    "dashscope", "siliconflow", "lambda", "featherless", "huggingface",
    "vercel", "scaleway", "ai21", "upstage", "inception",
    "qwen", "ltx",
    "aas",
]

_saved_keys = _load_json(KEYS_FILE, {})

API_KEYS = {}
for _p in _DEFAULT_KEY_PROVIDERS:
    if _p in _saved_keys and isinstance(_saved_keys[_p], list):
        API_KEYS[_p] = [k.strip() for k in _saved_keys[_p] if k and k.strip()]
    else:
        env_name = _p.upper() + "_API_KEYS"
        API_KEYS[_p] = [k.strip() for k in os.environ.get(env_name, "").split(",") if k.strip()]

for _p, _keys in _saved_keys.items():
    if _p not in API_KEYS and isinstance(_keys, list):
        API_KEYS[_p] = [k.strip() for k in _keys if k and k.strip()]

_key_cycles = {}

def _rebuild_cycle(provider):
    keys = API_KEYS.get(provider, [])
    _key_cycles[provider] = itertools.cycle(keys) if keys else None

for _p in API_KEYS:
    _rebuild_cycle(_p)

def _persist_keys():
    _save_json(KEYS_FILE, API_KEYS)

def set_keys(provider, keys):
    with _lock:
        API_KEYS[provider] = [k.strip() for k in keys if k and k.strip()]
        _rebuild_cycle(provider)
    _persist_keys()

def get_key_sequence(provider):
    with _lock:
        keys = list(API_KEYS.get(provider, []))
        cyc = _key_cycles.get(provider)
        if not keys or cyc is None:
            return []
        start = next(cyc)
        try:
            idx = keys.index(start)
        except ValueError:
            idx = 0
        return keys[idx:] + keys[:idx]

def _request_is_same_origin():
    origin = request.headers.get("Origin")
    if origin and urlparse(origin).netloc != request.host:
        return False
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        return False
    return True

@app.route("/api/keys", methods=["GET"])
def get_keys():
    if not _request_is_same_origin():
        return jsonify({"error": "Ключи доступны только странице, открытой с этого сервера"}), 403
    with _lock:
        keys = {p: list(v) for p, v in API_KEYS.items() if v}
    resp = jsonify({"keys": keys})
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.route("/api/keys", methods=["POST"])
def save_keys():
    body = request.get_json(force=True) or {}
    incoming = body.get("keys", {})
    saved_counts = {}
    for provider, keys in incoming.items():
        if not isinstance(keys, list):
            continue
        provider = (provider or "").strip().lower()
        if not provider:
            continue
        if provider not in API_KEYS:
            API_KEYS[provider] = []
            _rebuild_cycle(provider)
        set_keys(provider, keys)
        saved_counts[provider] = len(API_KEYS[provider])
    return jsonify({"saved": saved_counts})
