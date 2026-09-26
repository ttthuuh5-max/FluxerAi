import base64
import json
import requests
import time
from p05_keys import get_key_sequence
from p10_providers import ProviderError, RETRYABLE_STATUS

class GenerationError(Exception):
    pass

def _gen_call_with_rotation(provider, fn):
    keys = get_key_sequence(provider)
    if not keys:
        raise ProviderError(401, f"Нет ключей для '{provider}'")

    last_error = None
    for i, key in enumerate(keys):
        try:
            return fn(key)
        except ProviderError as e:
            last_error = e
            if e.status_code in RETRYABLE_STATUS and i < len(keys) - 1:
                continue
            if i < len(keys) - 1:
                continue
            raise
        except requests.RequestException as e:
            last_error = ProviderError(0, f"Сетевая ошибка: {e}")
            if i < len(keys) - 1:
                continue
            raise last_error
    if last_error:
        raise last_error
    raise ProviderError(500, f"Все ключи '{provider}' не сработали")

def _generate_image_google(key, prompt, model=None):
    model = model or "gemini-2.5-flash-image"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    resp = requests.post(
        url,
        json={"contents": [{"role": "user", "parts": [{"text": prompt}]}]},
        timeout=120,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, f"Google Gemini Image {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError):
        raise ProviderError(200, f"Не удалось разобрать ответ Gemini Image: {json.dumps(data)[:300]}")

    for part in parts:
        if "inlineData" in part:
            mime = part["inlineData"].get("mimeType", "image/png")
            ext = mime.split("/")[-1] if "/" in mime else "png"
            return {
                "name": f"generated_image.{ext}",
                "mime_type": mime,
                "data_base64": part["inlineData"].get("data", ""),
            }
    raise ProviderError(200, "Gemini Image не вернул изображение в ответе")

def _generate_image_openai(key, prompt, model=None):
    model = model or "gpt-image-1"
    url = "https://api.openai.com/v1/images/generations"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "prompt": prompt, "n": 1, "size": "1024x1024"},
        timeout=120,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, f"OpenAI Images {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    try:
        item = data["data"][0]
    except (KeyError, IndexError):
        raise ProviderError(200, f"Не удалось разобрать ответ OpenAI Images: {json.dumps(data)[:300]}")
    if "b64_json" in item:
        b64 = item["b64_json"]
    elif "url" in item:
        img_resp = requests.get(item["url"], timeout=60)
        if img_resp.status_code != 200:
            raise ProviderError(img_resp.status_code, "Не удалось скачать сгенерированную картинку по URL")
        b64 = base64.b64encode(img_resp.content).decode("ascii")
    else:
        raise ProviderError(200, "OpenAI Images вернул ответ без изображения")
    return {"name": "generated_image.png", "mime_type": "image/png", "data_base64": b64}

def _generate_image_cloudflare(key, prompt, model=None):
    if ":" not in key:
        raise ProviderError(
            401,
            "Ключ Cloudflare должен быть в формате 'account_id:api_token' "
            "(Account ID с dash.cloudflare.com, токен из My Profile → API Tokens → Workers AI)",
        )
    account_id, api_token = key.split(":", 1)
    model = model or "@cf/stabilityai/stable-diffusion-xl-base-1.0"
    if not model.startswith("@cf/"):
        model = f"@cf/{model}"
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
        json={"prompt": prompt},
        timeout=120,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, f"Cloudflare Workers AI {resp.status_code}: {resp.text[:300]}")
    content_type = resp.headers.get("Content-Type", "")
    if "application/json" in content_type:

        raise ProviderError(200, f"Cloudflare Workers AI вернул JSON вместо картинки: {resp.text[:300]}")
    mime = content_type.split(";")[0].strip() or "image/png"
    ext = mime.split("/")[-1] if "/" in mime else "png"
    b64 = base64.b64encode(resp.content).decode("ascii")
    return {"name": f"generated_image.{ext}", "mime_type": mime, "data_base64": b64}

def _generate_image_pollinations(key, prompt, model=None):
    model = model or "flux"
    encoded_prompt = requests.utils.quote(prompt, safe="")
    url = f"https://gen.pollinations.ai/image/{encoded_prompt}"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {key}"},
        params={"model": model, "width": 1024, "height": 1024},
        timeout=120,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, f"Pollinations Image {resp.status_code}: {resp.text[:300]}")
    mime = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    ext = mime.split("/")[-1] if "/" in mime else "jpg"
    b64 = base64.b64encode(resp.content).decode("ascii")
    return {"name": f"generated_image.{ext}", "mime_type": mime, "data_base64": b64}

IMAGE_GENERATION_PROVIDERS = [
    ("google", _generate_image_google),
    ("openai", _generate_image_openai),
    ("cloudflare", _generate_image_cloudflare),
    ("pollinations", _generate_image_pollinations),
]

IMAGE_MODELS = {
    "google": ["gemini-2.5-flash-image"],
    "openai": ["gpt-image-1", "dall-e-3"],
    "cloudflare": [
        "@cf/stabilityai/stable-diffusion-xl-base-1.0",
        "@cf/bytedance/stable-diffusion-xl-lightning",
        "@cf/black-forest-labs/flux-1-schnell",
    ],
    "pollinations": [
        "flux", "zimage", "gptimage", "kontext", "seedream5", "seedream",
        "nanobanana", "nanobanana-pro", "klein", "ideogram-v4-turbo",
        "qwen-image", "grok-imagine",
    ],
}

def generate_image(prompt, model=None, provider=None):
    providers_to_try = IMAGE_GENERATION_PROVIDERS
    if provider:
        match = [(p, fn) for p, fn in IMAGE_GENERATION_PROVIDERS if p == provider]
        if not match:
            raise GenerationError(f"Неизвестный провайдер изображений '{provider}' (доступны: google, openai, cloudflare, pollinations)")
        providers_to_try = match

    tried = []
    last_error = None
    for prov, fn in providers_to_try:
        if not get_key_sequence(prov):
            continue
        tried.append(prov)
        try:
            return _gen_call_with_rotation(prov, lambda key: fn(key, prompt, model))
        except ProviderError as e:
            last_error = e
            continue
    detail = f" ({last_error})" if last_error else ""
    raise GenerationError(
        f"Не удалось сгенерировать изображение (пробовал: {', '.join(tried) or 'нет ключей ни у одного провайдера изображений (google/openai/cloudflare/pollinations)'}){detail}"
    )

FAL_VIDEO_MODELS = [
    "fal-ai/veo3/fast",
    "fal-ai/veo3",
    "fal-ai/kling-video/v2/master/text-to-video",
    "fal-ai/luma-dream-machine",
]

def _generate_video_fal(key, prompt, model=None, duration=None):
    model_id = model or "fal-ai/veo3/fast"
    submit_url = f"https://queue.fal.run/{model_id}"
    payload = {"prompt": prompt}
    if duration:
        payload["duration"] = f"{int(duration)}s"
    resp = requests.post(
        submit_url,
        headers={"Authorization": f"Key {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    if resp.status_code not in (200, 202):
        raise ProviderError(resp.status_code, f"fal.ai submit {resp.status_code}: {resp.text[:300]}")
    submitted = resp.json()
    status_url = submitted.get("status_url")
    response_url = submitted.get("response_url")
    if not status_url or not response_url:
        raise ProviderError(200, f"fal.ai не вернул status_url/response_url: {json.dumps(submitted)[:300]}")

    max_wait_seconds = 300
    poll_interval = 4
    waited = 0
    while waited < max_wait_seconds:
        st = requests.get(status_url, headers={"Authorization": f"Key {key}"}, timeout=30)
        if st.status_code != 200:
            raise ProviderError(st.status_code, f"fal.ai status {st.status_code}: {st.text[:300]}")
        status_data = st.json()
        if status_data.get("status") == "COMPLETED":
            break
        if status_data.get("status") in ("ERROR", "FAILED"):
            raise ProviderError(500, f"fal.ai генерация видео провалилась: {json.dumps(status_data)[:300]}")
        time.sleep(poll_interval)
        waited += poll_interval
    else:
        raise ProviderError(504, "fal.ai не успел сгенерировать видео за отведённое время")

    res = requests.get(response_url, headers={"Authorization": f"Key {key}"}, timeout=30)
    if res.status_code != 200:
        raise ProviderError(res.status_code, f"fal.ai result {res.status_code}: {res.text[:300]}")
    result_data = res.json()
    video_url = None
    video_block = result_data.get("video")
    if isinstance(video_block, dict):
        video_url = video_block.get("url")
    if not video_url:
        raise ProviderError(200, f"fal.ai результат без video.url: {json.dumps(result_data)[:300]}")

    video_resp = requests.get(video_url, timeout=120)
    if video_resp.status_code != 200:
        raise ProviderError(video_resp.status_code, "Не удалось скачать сгенерированное видео с fal.ai")
    b64 = base64.b64encode(video_resp.content).decode("ascii")
    return {"name": "generated_video.mp4", "mime_type": "video/mp4", "data_base64": b64}

LTX_VIDEO_MODELS = [
    "ltx-2-3-fast",
    "ltx-2-3-pro",
]

def _generate_video_ltx(key, prompt, model=None, duration=None):
    model_id = model or "ltx-2-3-fast"
    resp = requests.post(
        "https://api.ltx.video/v1/text-to-video",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"prompt": prompt, "model": model_id, "duration": int(duration) if duration else 8, "resolution": "1920x1080"},
        timeout=300,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, f"LTX API {resp.status_code}: {resp.text[:300]}")
    if not resp.content:
        raise ProviderError(200, "LTX API вернул пустой ответ")
    b64 = base64.b64encode(resp.content).decode("ascii")
    return {"name": "generated_video.mp4", "mime_type": "video/mp4", "data_base64": b64}

def generate_video(prompt, model=None, provider=None, duration=None):
    provider = provider or "fal"
    if provider == "ltx":
        if not get_key_sequence("ltx"):
            raise GenerationError(
                "Не удалось сгенерировать видео: нет ключа LTX. Добавь ключ в правой панели (поле «LTX»)."
            )
        try:
            return _gen_call_with_rotation("ltx", lambda key: _generate_video_ltx(key, prompt, model, duration))
        except ProviderError as e:
            raise GenerationError(f"Не удалось сгенерировать видео через LTX: {e}")

    if not get_key_sequence("fal"):
        raise GenerationError(
            "Не удалось сгенерировать видео: нет ключа fal.ai. Добавь ключ в правой панели (поле «fal.ai»)."
        )
    try:
        return _gen_call_with_rotation("fal", lambda key: _generate_video_fal(key, prompt, model, duration))
    except ProviderError as e:
        raise GenerationError(f"Не удалось сгенерировать видео через fal.ai: {e}")

FISH_AUDIO_MODELS = [
    "s2.1-pro-free",
    "s1",
    "speech-1.6",
]

def _generate_audio_fish(key, text, model=None, voice_id=None):
    model = model or "s2.1-pro-free"
    url = "https://api.fish.audio/v1/tts"
    payload = {"text": text, "format": "mp3"}
    if voice_id:
        payload["reference_id"] = voice_id
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "model": model,
        },
        json=payload,
        timeout=120,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, f"Fish Audio {resp.status_code}: {resp.text[:300]}")
    b64 = base64.b64encode(resp.content).decode("ascii")
    return {"name": "generated_audio.mp3", "mime_type": "audio/mpeg", "data_base64": b64}

def generate_audio(text, model=None, voice_id=None):
    if not get_key_sequence("fish_audio"):
        raise GenerationError(
            "Не удалось сгенерировать аудио: нет ключа Fish Audio. Добавь ключ в правой панели (поле «Fish Audio»)."
        )
    try:
        return _gen_call_with_rotation("fish_audio", lambda key: _generate_audio_fish(key, text, model, voice_id))
    except ProviderError as e:
        raise GenerationError(f"Не удалось сгенерировать аудио через Fish Audio: {e}")


