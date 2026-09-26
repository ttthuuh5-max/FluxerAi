from flask import jsonify
from flask import request
from p01_app import app
from p03_storage import SETTINGS_FILE, _load_json, _save_json, _storage_lock

_DEFAULT_SETTINGS = {
    "language": "en",
    "tone": "neutral",
    "userName": "",
    "userAbout": "",
}

_settings_store = _load_json(SETTINGS_FILE, dict(_DEFAULT_SETTINGS))
for _k, _v in _DEFAULT_SETTINGS.items():
    _settings_store.setdefault(_k, _v)

def _persist_settings():
    _save_json(SETTINGS_FILE, _settings_store)

@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify(_settings_store)

@app.route("/api/settings", methods=["POST"])
def update_settings():
    body = request.get_json(force=True) or {}
    with _storage_lock:
        for key in ("language", "tone", "userName", "userAbout"):
            if key in body:
                _settings_store[key] = body[key]
    _persist_settings()
    return jsonify(_settings_store)

THINKING_MODELS = {
    "qwen": [
        "qwq-32b",
        "qwen3.8-max",
        "qwen3.6-plus",
    ],
    "anthropic": [
        "claude-3-7-sonnet",
        "claude-3-5-sonnet",
        "claude-opus-4",
        "claude-sonnet-4",
    ],
    "google": [
        "gemini-2.5-pro",
        "gemini-2.5-flash",
    ],
    "deepseek": [
        "deepseek-reasoner",
    ],
}

def is_thinking_model(provider, model):
    for pattern in THINKING_MODELS.get(provider, []):
        if pattern in model:
            return True
    return False

KNOWN_MODELS = {
    "qwen": ["qwen3.8-max", "qwen3.6-plus", "qwen-max", "qwen-plus", "qwen-turbo", "qwen3-coder-plus", "qwq-32b"],
    "google": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"],
    "openai": ["gpt-5", "gpt-5-mini", "gpt-4o", "gpt-4o-mini"],
    "anthropic": ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"],
    "openrouter": ["meta-llama/llama-3.3-70b", "deepseek/deepseek-v3", "mistralai/mistral-large"],
    "mistral": ["mistral-large-latest", "mistral-small-latest"],
    "xai": ["grok-4", "grok-4-mini"],
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],

    "grokified": ["grok-4.6", "grok-4.5", "grok-4.3", "grok-4.20-0309-reasoning", "grok-4.20-0309-non-reasoning", "grok-build-0.1"],
    "groq": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768", "gemma2-9b-it"],
    "perplexity": ["sonar-pro", "sonar", "sonar-reasoning-pro", "sonar-reasoning"],
    "together": ["meta-llama/Llama-3.3-70B-Instruct-Turbo", "Qwen/Qwen2.5-72B-Instruct-Turbo", "deepseek-ai/DeepSeek-V3"],
    "cohere": ["command-a-03-2025", "command-a-vision-07-2025", "command-r-plus-08-2024", "command-r-08-2024"],

    "cloudflare": [
        "@cf/meta/llama-3.1-8b-instruct",
        "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        "@cf/google/gemma-4-26b-a4b-it",
        "@cf/zai-org/glm-4.7-flash",
    ],

    "cerebras": ["llama-3.3-70b", "llama3.1-8b", "qwen-3-32b", "gpt-oss-120b"],
    "sambanova": ["Meta-Llama-3.3-70B-Instruct", "DeepSeek-V3-0324", "Qwen3-32B", "Llama-4-Maverick-17B-128E-Instruct"],
    "fireworks": ["accounts/fireworks/models/llama-v3p3-70b-instruct", "accounts/fireworks/models/deepseek-v3", "accounts/fireworks/models/qwen3-235b-a22b"],
    "nvidia": ["meta/llama-3.3-70b-instruct", "nvidia/llama-3.1-nemotron-70b-instruct", "deepseek-ai/deepseek-r1", "qwen/qwen2.5-coder-32b-instruct"],
    "hyperbolic": ["meta-llama/Llama-3.3-70B-Instruct", "deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"],
    "deepinfra": ["meta-llama/Llama-3.3-70B-Instruct", "deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"],
    "novita": ["meta-llama/llama-3.3-70b-instruct", "deepseek/deepseek-v3-0324", "qwen/qwen-2.5-72b-instruct"],
    "nebius": ["meta-llama/Llama-3.3-70B-Instruct", "deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"],
    "moonshot": ["kimi-k2-0905-preview", "kimi-k2-turbo-preview", "moonshot-v1-128k"],
    "zai": ["glm-4.6", "glm-4.5", "glm-4.5-air", "glm-4.5-flash"],
    "dashscope": ["qwen-max", "qwen-plus", "qwen-turbo", "qwen3-235b-a22b"],
    "siliconflow": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct", "THUDM/GLM-4-9B-0414"],
    "lambda": ["llama3.3-70b-instruct-fp8", "deepseek-v3-0324", "hermes3-405b"],
    "featherless": ["meta-llama/Llama-3.3-70B-Instruct", "Qwen/Qwen2.5-72B-Instruct", "mistralai/Mistral-Nemo-Instruct-2407"],
    "huggingface": ["meta-llama/Llama-3.3-70B-Instruct", "deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"],
    "vercel": ["anthropic/claude-sonnet-4.6", "openai/gpt-5.4-nano", "google/gemini-3.5-flash", "meta/llama-3.3-70b"],
    "scaleway": ["llama-3.3-70b-instruct", "deepseek-r1-distill-llama-70b", "qwen3-235b-a22b-instruct-2507"],
    "ai21": ["jamba-large", "jamba-mini"],
    "upstage": ["solar-pro2", "solar-mini"],
    "inception": ["mercury-2", "mercury-coder"],
}

PROVIDER_ATTACHMENT_SUPPORT = {
    "qwen":       {"image"},
    "google":     {"image", "video", "audio", "pdf"},
    "openai":     {"image", "pdf"},
    "anthropic":  {"image", "pdf"},
    "openrouter": {"image"},
    "mistral":    {"image"},
    "xai":        {"image"},
    "deepseek":   set(),

    "grokified":  {"image"},
    "groq":       {"image"},
    "perplexity": set(),
    "together":   set(),
    "cohere":     {"image"},
    "cloudflare": {"image"},

    "cerebras": set(),
    "sambanova": {"image"},
    "fireworks": {"image"},
    "nvidia": set(),
    "hyperbolic": {"image"},
    "deepinfra": {"image"},
    "novita": set(),
    "nebius": {"image"},
    "moonshot": set(),
    "zai": set(),
    "dashscope": {"image"},
    "siliconflow": set(),
    "lambda": set(),
    "featherless": set(),
    "huggingface": {"image"},
    "vercel": {"image"},
    "scaleway": set(),
    "ai21": set(),
    "upstage": set(),
    "inception": set(),
}

def classify_mime(mime_type, filename=""):
    mime_type = (mime_type or "").lower()
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("video/"):
        return "video"
    if mime_type.startswith("audio/"):
        return "audio"
    if mime_type == "application/pdf" or filename.lower().endswith(".pdf"):
        return "pdf"
    return "file"
