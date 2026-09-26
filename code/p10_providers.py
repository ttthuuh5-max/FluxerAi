import base64
import json
import requests
from p09_settings_models import PROVIDER_ATTACHMENT_SUPPORT, is_thinking_model
from p16_usage import record_usage

THINKING_BUDGET_TOKENS = 8000

RETRYABLE_STATUS = {401, 403, 429, 500, 502, 503, 529}

class ProviderError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code

def _unsupported_note(provider, attachments):
    supported = PROVIDER_ATTACHMENT_SUPPORT.get(provider, set())
    unsupported = [a for a in attachments if a.get("kind") not in supported]
    if not unsupported:
        return ""
    names = ", ".join(a["name"] for a in unsupported)
    return (
        f"\n\n[Внимание: модель провайдера '{provider}' не умеет напрямую читать "
        f"следующие вложения, они не были отправлены: {names}]"
    )

def call_google(model, messages, system_prompt, temperature, max_tokens, key, attachments, code_execution=False, web_search=False):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

    contents = []
    for i, m in enumerate(messages):
        role = "user" if m["role"] == "user" else "model"
        parts = [{"text": m["content"]}] if m.get("content") else []
        if i == len(messages) - 1 and role == "user" and attachments:
            for a in attachments:
                if a.get("kind") in PROVIDER_ATTACHMENT_SUPPORT["google"]:
                    parts.append({
                        "inline_data": {
                            "mime_type": a["mime_type"],
                            "data": a["data_base64"],
                        }
                    })
        if not parts:
            parts = [{"text": ""}]
        contents.append({"role": role, "parts": parts})

    payload = {
        "contents": contents,
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    if system_prompt:
        payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}

    tools = []
    if code_execution:

        tools.append({"code_execution": {}})
    if web_search:

        tools.append({"google_search": {}})
    if tools:
        payload["tools"] = tools

    resp = requests.post(url, json=payload, timeout=240)
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, f"Google API {resp.status_code}: {resp.text[:300]}")

    data = resp.json()

    record_usage(data.get("usageMetadata"), "google")
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError):
        raise ProviderError(200, f"Не удалось разобрать ответ Google: {json.dumps(data)[:300]}")

    text_chunks = []
    thinking_chunks = []
    files = []
    file_counter = 0
    for part in parts:
        if "thought" in part and part.get("thought"):

            thinking_chunks.append(part.get("text", ""))
        elif "text" in part:
            text_chunks.append(part["text"])
        elif "executableCode" in part:
            code = part["executableCode"].get("code", "")
            lang = part["executableCode"].get("language", "PYTHON")
            text_chunks.append(f"\n```{lang.lower()}\n{code}\n```\n")
        elif "codeExecutionResult" in part:
            outcome = part["codeExecutionResult"].get("outcome", "")
            output = part["codeExecutionResult"].get("output", "")
            marker = "⚠️ ошибка выполнения" if "FAIL" in outcome else "✅ вывод"
            text_chunks.append(f"\n**{marker}:**\n```\n{output}\n```\n")
        elif "inlineData" in part:

            file_counter += 1
            mime = part["inlineData"].get("mimeType", "application/octet-stream")
            ext = mime.split("/")[-1] if "/" in mime else "bin"
            files.append({
                "name": f"gemini_output_{file_counter}.{ext}",
                "mime_type": mime,
                "data_base64": part["inlineData"].get("data", ""),
            })

    text = "".join(text_chunks).strip() or ("(модель вернула только файл(ы), без текста)" if files else "")

    if thinking_chunks:
        thinking_text = "\n\n---\n".join(t.strip() for t in thinking_chunks if t.strip())
        if thinking_text:
            thinking_block = (
                f"<details>\n"
                f"<summary>🧠 Мышление модели</summary>\n\n"
                f"```\n{thinking_text}\n```\n"
                f"</details>\n\n"
            )
            text = thinking_block + text

    return text + _unsupported_note("google", attachments), files

def _openai_style_messages(messages, system_prompt, attachments, provider):
    msgs = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})

    supported = PROVIDER_ATTACHMENT_SUPPORT.get(provider, set())

    for i, m in enumerate(messages):
        is_last_user = (i == len(messages) - 1 and m["role"] == "user")
        if is_last_user and attachments:
            content_parts = []
            if m.get("content"):
                content_parts.append({"type": "text", "text": m["content"]})
            for a in attachments:
                kind = a.get("kind")
                if kind == "image" and "image" in supported:
                    data_url = f"data:{a['mime_type']};base64,{a['data_base64']}"
                    content_parts.append({"type": "image_url", "image_url": {"url": data_url}})
                elif kind == "pdf" and "pdf" in supported:
                    content_parts.append({
                        "type": "file",
                        "file": {
                            "filename": a["name"],
                            "file_data": f"data:{a['mime_type']};base64,{a['data_base64']}",
                        },
                    })
            if not content_parts:
                content_parts = [{"type": "text", "text": m.get("content", "")}]
            msgs.append({"role": "user", "content": content_parts})
        else:
            msgs.append({"role": m["role"], "content": m.get("content", "")})
    return msgs

def _openai_style_stream(url, headers, payload, provider_title, extra_json=None):
    """Общий генератор для любого OpenAI-совместимого /chat/completions с stream=True.
    yield-ит ('text', str) по мере прихода дельт, в конце ('usage', dict)."""
    payload = dict(payload)
    payload["stream"] = True
    if extra_json:
        payload.update(extra_json)

    resp = requests.post(url, headers=headers, json=payload, timeout=300, stream=True)
    if resp.status_code != 200:
        # Ошибка приходит не потоком — читаем тело целиком для сообщения.
        raise ProviderError(resp.status_code, f"{provider_title} API {resp.status_code}: {resp.text[:300]}")

    usage_raw = {}
    reasoning_open = False
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        raw = line[len("data:"):].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            chunk = json.loads(raw)
        except ValueError:
            continue
        if chunk.get("usage"):
            usage_raw.update(chunk["usage"])
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning:
            if not reasoning_open:
                reasoning_open = True
                yield ("text", "<details>\n<summary>🧠 Мышление модели</summary>\n\n```\n")
            yield ("text", reasoning)
        content = delta.get("content")
        if content:
            if reasoning_open:
                reasoning_open = False
                yield ("text", "\n```\n</details>\n\n")
            yield ("text", content)

    if reasoning_open:
        yield ("text", "\n```\n</details>\n\n")
    yield ("usage", usage_raw)

def call_openai_stream(model, messages, system_prompt, temperature, max_tokens, key, attachments, code_execution=False, web_search=False):
    url = "https://api.openai.com/v1/chat/completions"
    msgs = _openai_style_messages(messages, system_prompt, attachments, "openai")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens}
    for kind, val in _openai_style_stream(url, headers, payload, "OpenAI"):
        if kind == "usage":
            yield ("usage_typed", (val, "openai"))
        else:
            yield (kind, val)
    yield ("files", [])
    note = _unsupported_note("openai", attachments)
    if note:
        yield ("text", note)

def call_openai(model, messages, system_prompt, temperature, max_tokens, key, attachments, code_execution=False, web_search=False):
    url = "https://api.openai.com/v1/chat/completions"
    msgs = _openai_style_messages(messages, system_prompt, attachments, "openai")

    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens},
        timeout=180,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, f"OpenAI API {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    record_usage(data.get("usage"), "openai")
    text = data["choices"][0]["message"]["content"]
    return text + _unsupported_note("openai", attachments), []

def call_anthropic_stream(model, messages, system_prompt, temperature, max_tokens, key, attachments, code_execution=False, web_search=False):
    """Генератор: yield-ит куски текста ('text', str) по мере прихода от Anthropic,
    в конце — ('usage', dict) и ('files', list). Формат SSE Anthropic: строки
    'event: ...' и 'data: {...}' блоками, разделёнными пустой строкой."""
    url = "https://api.anthropic.com/v1/messages"

    supported = PROVIDER_ATTACHMENT_SUPPORT["anthropic"]
    out_messages = []
    for i, m in enumerate(messages):
        is_last_user = (i == len(messages) - 1 and m["role"] == "user")
        if is_last_user and attachments:
            blocks = []
            for a in attachments:
                kind = a.get("kind")
                if kind == "image" and "image" in supported:
                    blocks.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": a["mime_type"], "data": a["data_base64"]},
                    })
                elif kind == "pdf" and "pdf" in supported:
                    blocks.append({
                        "type": "document",
                        "source": {"type": "base64", "media_type": a["mime_type"], "data": a["data_base64"]},
                    })
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            out_messages.append({"role": "user", "content": blocks or [{"type": "text", "text": ""}]})
        else:
            out_messages.append({"role": m["role"], "content": m.get("content", "")})

    use_thinking = is_thinking_model("anthropic", model)

    payload = {
        "model": model,
        "system": system_prompt or "",
        "messages": out_messages,
        "max_tokens": max_tokens,
        "stream": True,
    }
    if use_thinking:
        payload["thinking"] = {
            "type": "enabled",
            "budget_tokens": min(THINKING_BUDGET_TOKENS, max(1024, max_tokens - 1000)),
        }
        payload["temperature"] = 1
    else:
        payload["temperature"] = temperature

    resp = requests.post(
        url,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
        json=payload,
        timeout=300,
        stream=True,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, f"Anthropic API {resp.status_code}: {resp.text[:300]}")

    thinking_open = False
    thinking_any = False
    usage_raw = {}
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        raw = line[len("data:"):].strip()
        if not raw:
            continue
        try:
            evt = json.loads(raw)
        except ValueError:
            continue
        etype = evt.get("type")
        if etype == "content_block_start":
            block = evt.get("content_block", {})
            if block.get("type") == "thinking":
                thinking_open = True
                thinking_any = True
                yield ("text", "<details>\n<summary>🧠 Мышление модели</summary>\n\n```\n")
        elif etype == "content_block_delta":
            delta = evt.get("delta", {})
            dtype = delta.get("type")
            if dtype == "text_delta":
                yield ("text", delta.get("text", ""))
            elif dtype == "thinking_delta":
                yield ("text", delta.get("thinking", ""))
        elif etype == "content_block_stop":
            if thinking_open:
                thinking_open = False
                yield ("text", "\n```\n</details>\n\n")
        elif etype == "message_delta":
            u = evt.get("usage")
            if u:
                usage_raw.update(u)
        elif etype == "message_start":
            u = (evt.get("message") or {}).get("usage")
            if u:
                usage_raw.update(u)

    yield ("usage", usage_raw)
    yield ("files", [])

def call_anthropic(model, messages, system_prompt, temperature, max_tokens, key, attachments, code_execution=False, web_search=False):
    url = "https://api.anthropic.com/v1/messages"

    supported = PROVIDER_ATTACHMENT_SUPPORT["anthropic"]
    out_messages = []
    for i, m in enumerate(messages):
        is_last_user = (i == len(messages) - 1 and m["role"] == "user")
        if is_last_user and attachments:
            blocks = []
            for a in attachments:
                kind = a.get("kind")
                if kind == "image" and "image" in supported:
                    blocks.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": a["mime_type"], "data": a["data_base64"]},
                    })
                elif kind == "pdf" and "pdf" in supported:
                    blocks.append({
                        "type": "document",
                        "source": {"type": "base64", "media_type": a["mime_type"], "data": a["data_base64"]},
                    })
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            out_messages.append({"role": "user", "content": blocks or [{"type": "text", "text": ""}]})
        else:
            out_messages.append({"role": m["role"], "content": m.get("content", "")})

    use_thinking = is_thinking_model("anthropic", model)

    payload = {
        "model": model,
        "system": system_prompt or "",
        "messages": out_messages,
        "max_tokens": max_tokens,
    }

    if use_thinking:

        payload["thinking"] = {
            "type": "enabled",
            "budget_tokens": min(THINKING_BUDGET_TOKENS, max(1024, max_tokens - 1000)),
        }
        payload["temperature"] = 1
    else:
        payload["temperature"] = temperature

    resp = requests.post(
        url,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
        json=payload,
        timeout=300,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, f"Anthropic API {resp.status_code}: {resp.text[:300]}")

    adata = resp.json()

    record_usage(adata.get("usage"), "anthropic")
    content_blocks = adata.get("content", [])

    thinking_parts = []
    text_parts = []
    for block in content_blocks:
        btype = block.get("type")
        if btype == "thinking":
            thinking_parts.append(block.get("thinking", ""))
        elif btype == "text":
            text_parts.append(block.get("text", ""))

    text = "".join(text_parts)

    if thinking_parts:
        thinking_text = "\n\n---\n".join(thinking_parts).strip()
        thinking_block = (
            f"<details>\n"
            f"<summary>🧠 Мышление модели</summary>\n\n"
            f"```\n{thinking_text}\n```\n"
            f"</details>\n\n"
        )
        text = thinking_block + text

    return text + _unsupported_note("anthropic", attachments), []

def _make_simple_openai_stream(provider, url, title, use_style_messages=True):
    def _stream(model, messages, system_prompt, temperature, max_tokens, key, attachments, code_execution=False, web_search=False):
        if use_style_messages:
            msgs = _openai_style_messages(messages, system_prompt, attachments, provider)
        else:
            msgs = []
            if system_prompt:
                msgs.append({"role": "system", "content": system_prompt})
            msgs.extend({"role": m["role"], "content": m.get("content", "")} for m in messages)
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens}
        for kind, val in _openai_style_stream(url, headers, payload, title):
            if kind == "usage":
                yield ("usage_typed", (val, "openai"))
            else:
                yield (kind, val)
        yield ("files", [])
        note = _unsupported_note(provider, attachments)
        if note:
            yield ("text", note)
    return _stream

def call_openrouter(model, messages, system_prompt, temperature, max_tokens, key, attachments, code_execution=False, web_search=False):
    url = "https://openrouter.ai/api/v1/chat/completions"
    msgs = _openai_style_messages(messages, system_prompt, attachments, "openrouter")

    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens},
        timeout=180,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, f"OpenRouter API {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    record_usage(data.get("usage"), "openai")
    text = data["choices"][0]["message"]["content"]
    return text + _unsupported_note("openrouter", attachments), []

call_openrouter_stream = _make_simple_openai_stream("openrouter", "https://openrouter.ai/api/v1/chat/completions", "OpenRouter")

def call_mistral(model, messages, system_prompt, temperature, max_tokens, key, attachments, code_execution=False, web_search=False):
    url = "https://api.mistral.ai/v1/chat/completions"
    msgs = _openai_style_messages(messages, system_prompt, attachments, "mistral")

    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens},
        timeout=180,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, f"Mistral API {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    record_usage(data.get("usage"), "openai")
    text = data["choices"][0]["message"]["content"]
    return text + _unsupported_note("mistral", attachments), []

call_mistral_stream = _make_simple_openai_stream("mistral", "https://api.mistral.ai/v1/chat/completions", "Mistral")

def call_xai(model, messages, system_prompt, temperature, max_tokens, key, attachments, code_execution=False, web_search=False):
    url = "https://api.x.ai/v1/chat/completions"
    msgs = _openai_style_messages(messages, system_prompt, attachments, "xai")

    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens},
        timeout=180,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, f"xAI API {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    record_usage(data.get("usage"), "openai")
    text = data["choices"][0]["message"]["content"]
    return text + _unsupported_note("xai", attachments), []

call_xai_stream = _make_simple_openai_stream("xai", "https://api.x.ai/v1/chat/completions", "xAI")

def call_grokified(model, messages, system_prompt, temperature, max_tokens, key, attachments, code_execution=False, web_search=False):
    url = "https://api.grokified.com/v1/chat/completions"
    msgs = _openai_style_messages(messages, system_prompt, attachments, "grokified")

    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens},
        timeout=180,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, f"Grokified API {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    record_usage(data.get("usage"), "openai")
    text = data["choices"][0]["message"]["content"]
    return text + _unsupported_note("grokified", attachments), []

call_grokified_stream = _make_simple_openai_stream("grokified", "https://api.grokified.com/v1/chat/completions", "Grokified")

def call_deepseek(model, messages, system_prompt, temperature, max_tokens, key, attachments, code_execution=False, web_search=False):
    url = "https://api.deepseek.com/chat/completions"
    msgs = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})
    msgs.extend({"role": m["role"], "content": m.get("content", "")} for m in messages)

    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens},
        timeout=300,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, f"DeepSeek API {resp.status_code}: {resp.text[:300]}")

    ddata = resp.json()
    record_usage(ddata.get("usage"), "openai")
    message = ddata["choices"][0]["message"]
    text = message.get("content") or ""
    reasoning = message.get("reasoning_content") or ""

    if reasoning.strip():
        thinking_block = (
            f"<details>\n"
            f"<summary>🧠 Мышление модели</summary>\n\n"
            f"```\n{reasoning.strip()}\n```\n"
            f"</details>\n\n"
        )
        text = thinking_block + text

    return text + _unsupported_note("deepseek", attachments), []

call_deepseek_stream = _make_simple_openai_stream("deepseek", "https://api.deepseek.com/chat/completions", "DeepSeek", use_style_messages=False)

def call_groq(model, messages, system_prompt, temperature, max_tokens, key, attachments, code_execution=False, web_search=False):
    url = "https://api.groq.com/openai/v1/chat/completions"
    msgs = _openai_style_messages(messages, system_prompt, attachments, "groq")

    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens},
        timeout=180,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, f"Groq API {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    record_usage(data.get("usage"), "openai")
    text = data["choices"][0]["message"]["content"]
    return text + _unsupported_note("groq", attachments), []

call_groq_stream = _make_simple_openai_stream("groq", "https://api.groq.com/openai/v1/chat/completions", "Groq")

def call_perplexity(model, messages, system_prompt, temperature, max_tokens, key, attachments, code_execution=False, web_search=False):
    url = "https://api.perplexity.ai/chat/completions"
    msgs = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})
    msgs.extend({"role": m["role"], "content": m.get("content", "")} for m in messages)

    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens},
        timeout=180,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, f"Perplexity API {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    record_usage(data.get("usage"), "openai")
    text = data["choices"][0]["message"]["content"]
    return text + _unsupported_note("perplexity", attachments), []

call_perplexity_stream = _make_simple_openai_stream("perplexity", "https://api.perplexity.ai/chat/completions", "Perplexity", use_style_messages=False)

def call_together(model, messages, system_prompt, temperature, max_tokens, key, attachments, code_execution=False, web_search=False):
    url = "https://api.together.xyz/v1/chat/completions"
    msgs = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})
    msgs.extend({"role": m["role"], "content": m.get("content", "")} for m in messages)

    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens},
        timeout=180,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, f"Together API {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    record_usage(data.get("usage"), "openai")
    text = data["choices"][0]["message"]["content"]
    return text + _unsupported_note("together", attachments), []

call_together_stream = _make_simple_openai_stream("together", "https://api.together.xyz/v1/chat/completions", "Together", use_style_messages=False)

def call_cohere(model, messages, system_prompt, temperature, max_tokens, key, attachments, code_execution=False, web_search=False):
    url = "https://api.cohere.com/v2/chat"
    msgs = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})

    supported = PROVIDER_ATTACHMENT_SUPPORT.get("cohere", set())
    for i, m in enumerate(messages):
        is_last_user = (i == len(messages) - 1 and m["role"] == "user")
        if is_last_user and attachments:
            content_parts = []
            if m.get("content"):
                content_parts.append({"type": "text", "text": m["content"]})
            for a in attachments:
                if a.get("kind") == "image" and "image" in supported:
                    data_url = f"data:{a['mime_type']};base64,{a['data_base64']}"
                    content_parts.append({"type": "image_url", "image_url": {"url": data_url}})
            if not content_parts:
                content_parts = [{"type": "text", "text": m.get("content", "")}]
            msgs.append({"role": "user", "content": content_parts})
        else:
            msgs.append({"role": m["role"], "content": m.get("content", "")})

    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens},
        timeout=180,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, f"Cohere API {resp.status_code}: {resp.text[:300]}")
    data = resp.json()

    record_usage(data.get("usage"), "cohere")
    content_blocks = data.get("message", {}).get("content", [])
    text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
    return text + _unsupported_note("cohere", attachments), []

def call_cloudflare(model, messages, system_prompt, temperature, max_tokens, key, attachments, code_execution=False, web_search=False):
    if ":" not in key:
        raise ProviderError(401, "Ключ Cloudflare должен быть в формате 'account_id:api_token'")
    account_id, api_token = key.split(":", 1)
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"

    supported = PROVIDER_ATTACHMENT_SUPPORT.get("cloudflare", set())
    msgs = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})
    msgs.extend({"role": m["role"], "content": m.get("content", "")} for m in messages)

    payload = {"messages": msgs, "temperature": temperature, "max_tokens": max_tokens}

    last_user_attachments = attachments if messages and messages[-1]["role"] == "user" else []
    image_attachment = next(
        (a for a in last_user_attachments if a.get("kind") == "image" and "image" in supported),
        None,
    )
    if image_attachment:
        raw_bytes = base64.b64decode(image_attachment["data_base64"])
        payload["image"] = list(raw_bytes)

    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
        json=payload,
        timeout=180,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, f"Cloudflare API {resp.status_code}: {resp.text[:300]}")
    data = resp.json()

    record_usage((data.get("result") or {}).get("usage"), "openai")
    text = data.get("result", {}).get("response", "") or json.dumps(data.get("result", {}))[:500]
    return text + _unsupported_note("cloudflare", attachments), []

OPENAI_COMPAT_ENDPOINTS = {
    'qwen': ('Qwen (Alibaba)', 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions'),
    'cerebras': ('Cerebras', 'https://api.cerebras.ai/v1/chat/completions'),
    'sambanova': ('SambaNova', 'https://api.sambanova.ai/v1/chat/completions'),
    'fireworks': ('Fireworks AI', 'https://api.fireworks.ai/inference/v1/chat/completions'),
    'nvidia': ('NVIDIA NIM', 'https://integrate.api.nvidia.com/v1/chat/completions'),
    'hyperbolic': ('Hyperbolic', 'https://api.hyperbolic.xyz/v1/chat/completions'),
    'deepinfra': ('DeepInfra', 'https://api.deepinfra.com/v1/openai/chat/completions'),
    'novita': ('Novita AI', 'https://api.novita.ai/v3/openai/chat/completions'),
    'nebius': ('Nebius AI Studio', 'https://api.tokenfactory.nebius.com/v1/chat/completions'),
    'moonshot': ('Moonshot AI (Kimi)', 'https://api.moonshot.ai/v1/chat/completions'),
    'zai': ('Z.AI (Zhipu GLM)', 'https://api.z.ai/api/paas/v4/chat/completions'),
    'dashscope': ('Alibaba Qwen (DashScope)', 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions'),
    'siliconflow': ('SiliconFlow', 'https://api.siliconflow.com/v1/chat/completions'),
    'lambda': ('Lambda Inference', 'https://api.lambdalabs.com/v1/chat/completions'),
    'featherless': ('Featherless AI', 'https://api.featherless.ai/v1/chat/completions'),
    'huggingface': ('Hugging Face Router', 'https://router.huggingface.co/v1/chat/completions'),
    'vercel': ('Vercel AI Gateway', 'https://ai-gateway.vercel.sh/v1/chat/completions'),
    'scaleway': ('Scaleway Generative APIs', 'https://api.scaleway.ai/v1/chat/completions'),
    'ai21': ('AI21 Labs (Jamba)', 'https://api.ai21.com/studio/v1/chat/completions'),
    'upstage': ('Upstage (Solar)', 'https://api.upstage.ai/v1/solar/chat/completions'),
    'inception': ('Inception Labs (Mercury)', 'https://api.inceptionlabs.ai/v1/chat/completions'),
}

import os as _os
if _os.environ.get("QWEN_BASE_URL"):
    _t = OPENAI_COMPAT_ENDPOINTS["qwen"][0]
    OPENAI_COMPAT_ENDPOINTS["qwen"] = (
        _t, _os.environ["QWEN_BASE_URL"].rstrip("/") + "/chat/completions"
    )
if _os.environ.get("DASHSCOPE_BASE_URL"):
    _t = OPENAI_COMPAT_ENDPOINTS["dashscope"][0]
    OPENAI_COMPAT_ENDPOINTS["dashscope"] = (
        _t, _os.environ["DASHSCOPE_BASE_URL"].rstrip("/") + "/chat/completions"
    )

OPENAI_COMPAT_MODELS_URLS = {

}

def _extract_reasoning(message):
    for field in ("reasoning_content", "reasoning"):
        val = message.get(field)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""

def _openai_compat_call(provider, model, messages, system_prompt, temperature,
                        max_tokens, key, attachments):
    title, url = OPENAI_COMPAT_ENDPOINTS[provider]
    msgs = _openai_style_messages(messages, system_prompt, attachments, provider)

    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens},
        timeout=240,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, f"{title} API {resp.status_code}: {resp.text[:300]}")

    try:
        cdata = resp.json()
        message = cdata["choices"][0]["message"]
    except (KeyError, IndexError, ValueError):
        raise ProviderError(200, f"Не удалось разобрать ответ {title}: {resp.text[:300]}")
    record_usage(cdata.get("usage"), "openai")

    text = message.get("content") or ""
    reasoning = _extract_reasoning(message)
    if reasoning:
        text = (
            "<details>\n<summary>🧠 Мышление модели</summary>\n\n"
            f"```\n{reasoning}\n```\n</details>\n\n" + text
        )
    return text + _unsupported_note(provider, attachments), []

def _openai_compat_stream(provider, model, messages, system_prompt, temperature, max_tokens, key, attachments):
    title, url = OPENAI_COMPAT_ENDPOINTS[provider]
    msgs = _openai_style_messages(messages, system_prompt, attachments, provider)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens}
    for kind, val in _openai_style_stream(url, headers, payload, title):
        if kind == "usage":
            yield ("usage_typed", (val, "openai"))
        else:
            yield (kind, val)
    yield ("files", [])
    note = _unsupported_note(provider, attachments)
    if note:
        yield ("text", note)

def _make_openai_compat(provider):
    def _call(model, messages, system_prompt, temperature, max_tokens, key,
              attachments, code_execution=False, web_search=False):
        return _openai_compat_call(provider, model, messages, system_prompt,
                                   temperature, max_tokens, key, attachments)
    _call.__name__ = f"call_{provider}"
    _call.__doc__ = f"{OPENAI_COMPAT_ENDPOINTS[provider][0]} — OpenAI-совместимый эндпоинт."
    return _call

def _make_openai_compat_stream(provider):
    def _call(model, messages, system_prompt, temperature, max_tokens, key,
              attachments, code_execution=False, web_search=False):
        return _openai_compat_stream(provider, model, messages, system_prompt,
                                     temperature, max_tokens, key, attachments)
    _call.__name__ = f"call_{provider}_stream"
    return _call

for _pid in OPENAI_COMPAT_ENDPOINTS:
    globals()[f"call_{_pid}"] = _make_openai_compat(_pid)
    globals()[f"call_{_pid}_stream"] = _make_openai_compat_stream(_pid)

PROVIDERS = {
    "google": call_google,
    "openai": call_openai,
    "anthropic": call_anthropic,
    "openrouter": call_openrouter,
    "mistral": call_mistral,
    "xai": call_xai,
    "deepseek": call_deepseek,
    "grokified": call_grokified,
    "groq": call_groq,
    "perplexity": call_perplexity,
    "together": call_together,
    "cohere": call_cohere,
    "cloudflare": call_cloudflare,
}

for _pid in OPENAI_COMPAT_ENDPOINTS:
    PROVIDERS[_pid] = globals()[f"call_{_pid}"]

# Провайдеры, для которых реализован настоящий потоковый вывод (SSE): текст
# приходит клиенту по мере генерации, а не одним куском в конце. Для остальных
# (google, cohere, cloudflare, local) стриминг эмулируется на уровне routing —
# ответ ждётся целиком, но клиент всё равно получает его через тот же канал.
STREAMING_PROVIDERS = {
    "openai": call_openai_stream,
    "anthropic": call_anthropic_stream,
    "openrouter": call_openrouter_stream,
    "mistral": call_mistral_stream,
    "xai": call_xai_stream,
    "grokified": call_grokified_stream,
    "deepseek": call_deepseek_stream,
    "groq": call_groq_stream,
    "perplexity": call_perplexity_stream,
    "together": call_together_stream,
}
for _pid in OPENAI_COMPAT_ENDPOINTS:
    STREAMING_PROVIDERS[_pid] = globals()[f"call_{_pid}_stream"]
