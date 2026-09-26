from datetime import datetime
from datetime import timezone
import base64
import ipaddress
import json
import mimetypes
import re
import socket
from urllib.parse import urlparse
import requests
from p08_agents import MAX_AGENTS, _agent_by_slot, _agents_store
from p10_providers import PROVIDERS, ProviderError
from p18_video import tool_video_edit, VIDEO_TOOL_DESCRIPTION, ffmpeg_available

TOOL_CALL_RE = re.compile(r"^call_tool:\s*([a-zA-Z0-9_]+)\s*$", re.MULTILINE)

TOOL_CALL_WITH_ARGS_RE = re.compile(r"^call_tool:\s*([a-zA-Z0-9_]+)\s*(\{.*\})\s*$", re.MULTILINE)

from config.settings import MAX_TOOL_ROUNDS

# --- Настройки веб-инструментов (web_fetch / web_download) ---
WEB_TOOL_TIMEOUT_SECONDS = 50
WEB_DOWNLOAD_MAX_BYTES = 100 * 1024 * 1024  # 100 МБ
WEB_FETCH_MAX_CHARS = 20000  # чтобы не забивать контекст модели целиком HTML-простынёй
WEB_TOOL_USER_AGENT = "FluxerAi-WebTool/1.0 (+local self-hosted assistant)"


class WebToolBlockedError(Exception):
    """Запрос заблокирован политикой безопасности (локальный/приватный адрес и т.п.)."""


def _resolve_and_check_host(hostname):
    """Резолвит hostname и проверяет, что ни один из адресов не приватный/локальный.
    Бросает WebToolBlockedError, если адрес запрещён."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        raise WebToolBlockedError(f"не удалось определить адрес хоста '{hostname}': {e}")

    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified
        ):
            raise WebToolBlockedError(
                f"адрес '{hostname}' ({ip_str}) указывает на локальную/внутреннюю сеть — запрещено"
            )


def _validate_public_url(url):
    """Проверяет URL перед обращением: только http/https, хост не пустой,
    хост не резолвится в приватный/локальный адрес. Возвращает распарсенный URL."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise WebToolBlockedError("разрешены только http:// и https:// ссылки")
    if not parsed.hostname:
        raise WebToolBlockedError("в ссылке не указан хост")
    if parsed.hostname.lower() in ("localhost",):
        raise WebToolBlockedError("обращение к localhost запрещено")
    _resolve_and_check_host(parsed.hostname)
    return parsed


def tool_web_fetch(request_obj, body, args=None):
    """Скачивает страницу по URL и возвращает её текстовое содержимое (без тегов),
    обрезанное до разумного объёма, чтобы не забить контекст модели."""
    args = args or {}
    url = (args.get("url") or "").strip()
    if not url:
        return "ошибка: нужно поле url"

    try:
        _validate_public_url(url)
    except WebToolBlockedError as e:
        return f"ошибка: запрос заблокирован — {e}"

    try:
        resp = requests.get(
            url,
            timeout=WEB_TOOL_TIMEOUT_SECONDS,
            headers={"User-Agent": WEB_TOOL_USER_AGENT},
            stream=True,
            allow_redirects=True,
        )
    except requests.exceptions.Timeout:
        return f"ошибка: запрос превысил таймаут ({WEB_TOOL_TIMEOUT_SECONDS} сек)"
    except requests.exceptions.RequestException as e:
        return f"ошибка запроса: {e}"

    # Ограничиваем скачиваемый объём даже для чтения страниц (та же защита, что и для download).
    content_bytes = b""
    try:
        for chunk in resp.iter_content(chunk_size=65536):
            content_bytes += chunk
            if len(content_bytes) > WEB_DOWNLOAD_MAX_BYTES:
                resp.close()
                return f"ошибка: страница больше лимита в {WEB_DOWNLOAD_MAX_BYTES // (1024*1024)} МБ"
    except requests.exceptions.RequestException as e:
        return f"ошибка при получении содержимого: {e}"

    status = resp.status_code
    content_type = (resp.headers.get("Content-Type") or "").lower()

    if status >= 400:
        return f"ошибка: сервер ответил статусом {status} для {url}"

    if "text/html" in content_type or "application/xhtml" in content_type:
        html_text = content_bytes.decode(resp.encoding or "utf-8", errors="replace")
        text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", html_text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
    elif content_type.startswith("text/") or "json" in content_type or "xml" in content_type:
        text = content_bytes.decode(resp.encoding or "utf-8", errors="replace").strip()
    else:
        return (
            f"содержимое по адресу {url} — не текст/HTML (Content-Type: {content_type or 'неизвестен'}, "
            f"{len(content_bytes)} байт). Для скачивания файла используй call_tool: web_download."
        )

    truncated = False
    if len(text) > WEB_FETCH_MAX_CHARS:
        text = text[:WEB_FETCH_MAX_CHARS]
        truncated = True

    note = " [текст обрезан по лимиту символов]" if truncated else ""
    return f"страница {url} (статус {status}):\n{text}{note}"


def tool_web_download(request_obj, body, args=None):
    """Скачивает файл по прямой ссылке и возвращает его как готовое вложение
    (base64), которое дальше уходит пользователю так же, как send_file."""
    args = args or {}
    url = (args.get("url") or "").strip()
    filename = (args.get("filename") or "").strip()
    if not url:
        return "ошибка: нужно поле url", []

    try:
        _validate_public_url(url)
    except WebToolBlockedError as e:
        return f"ошибка: запрос заблокирован — {e}", []

    try:
        resp = requests.get(
            url,
            timeout=WEB_TOOL_TIMEOUT_SECONDS,
            headers={"User-Agent": WEB_TOOL_USER_AGENT},
            stream=True,
            allow_redirects=True,
        )
    except requests.exceptions.Timeout:
        return f"ошибка: запрос превысил таймаут ({WEB_TOOL_TIMEOUT_SECONDS} сек)", []
    except requests.exceptions.RequestException as e:
        return f"ошибка запроса: {e}", []

    if resp.status_code >= 400:
        return f"ошибка: сервер ответил статусом {resp.status_code} для {url}", []

    content_length = resp.headers.get("Content-Length")
    if content_length and int(content_length) > WEB_DOWNLOAD_MAX_BYTES:
        return (
            f"ошибка: файл весит {int(content_length) // (1024*1024)} МБ, "
            f"это больше лимита в {WEB_DOWNLOAD_MAX_BYTES // (1024*1024)} МБ",
            [],
        )

    content = bytearray()
    try:
        for chunk in resp.iter_content(chunk_size=1024 * 256):
            content.extend(chunk)
            if len(content) > WEB_DOWNLOAD_MAX_BYTES:
                resp.close()
                return (
                    f"ошибка: файл больше лимита в {WEB_DOWNLOAD_MAX_BYTES // (1024*1024)} МБ "
                    "(остановлено во время скачивания)",
                    [],
                )
    except requests.exceptions.RequestException as e:
        return f"ошибка при скачивании: {e}", []

    if not filename:
        parsed_path = urlparse(url).path
        filename = parsed_path.rsplit("/", 1)[-1] or "downloaded_file"
    if "." not in filename:
        guessed_ext = mimetypes.guess_extension((resp.headers.get("Content-Type") or "").split(";")[0].strip())
        if guessed_ext:
            filename += guessed_ext

    mime_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip() or "application/octet-stream"
    data_b64 = base64.b64encode(bytes(content)).decode("ascii")

    file_info = {"name": filename, "mime_type": mime_type, "data_base64": data_b64}
    size_kb = len(content) / 1024
    result_text = f"файл «{filename}» скачан ({size_kb:.1f} КБ, {mime_type}) — приложен к сообщению."
    return result_text, [file_info]


def tool_get_time(request_obj, body):

    now_utc = datetime.now(timezone.utc)
    parts = [f"текущее время (UTC): {now_utc.strftime('%Y-%m-%d %H:%M:%S')}"]
    client_time_iso = body.get("client_time")
    client_timezone = body.get("client_timezone")
    if client_time_iso:
        parts.append(f"локальное время пользователя: {client_time_iso}")
    if client_timezone:
        parts.append(f"часовой пояс пользователя: {client_timezone}")
    return "; ".join(parts)

def tool_connect_telegram_bot(request_obj, body):
    # Сам инструмент ничего не делает на сервере — он лишь просит фронтенд
    # показать окно «введите токен бота». Дальше пользователь вставляет
    # токен, и фронтенд сам обращается к /api/telegram/connect. Разбор
    # ui_action на стороне клиента — в p14_chat._run_stream_with_tools
    # (событие tool_round несёт список имён; фронтенд по имени этого
    # инструмента открывает модалку).
    return (
        "окно для ввода токена Telegram-бота показано пользователю на экране. "
        "Дальше ничего делать не нужно — как только пользователь вставит токен, "
        "сервер сам подключит бота."
    )

def tool_connect_discord_bot(request_obj, body):
    # Как и connect_telegram_bot — сам инструмент ничего не делает на сервере,
    # он лишь просит фронтенд показать окно «введите токен Discord-бота».
    return (
        "окно для ввода токена Discord-бота показано пользователю на экране. "
        "Дальше ничего делать не нужно — как только пользователь вставит токен, "
        "сервер сам подключит бота."
    )

def tool_get_user_ip(request_obj, body):
    xff = request_obj.headers.get("X-Forwarded-For", "")
    if xff:

        ip = xff.split(",")[0].strip()
        if ip:
            return ip
    x_real_ip = request_obj.headers.get("X-Real-IP", "")
    if x_real_ip:
        return x_real_ip.strip()
    return request_obj.remote_addr or "неизвестен (адрес не определён сервером)"

def _codec_encode(fmt, text):
    if fmt == "base64":
        return base64.b64encode(text.encode("utf-8")).decode("ascii")
    if fmt == "url":
        from urllib.parse import quote
        return quote(text, safe="")
    if fmt == "hex":
        return text.encode("utf-8").hex()
    if fmt == "rot13":
        import codecs
        return codecs.encode(text, "rot_13")
    raise ValueError(f"неизвестный формат кодирования: {fmt}")

def _codec_decode(fmt, text):
    if fmt == "base64":
        padded = text + "=" * (-len(text) % 4)
        return base64.b64decode(padded).decode("utf-8", errors="replace")
    if fmt == "url":
        from urllib.parse import unquote
        return unquote(text)
    if fmt == "hex":
        clean = text.replace(" ", "").replace("\n", "")
        return bytes.fromhex(clean).decode("utf-8", errors="replace")
    if fmt == "rot13":
        import codecs
        return codecs.encode(text, "rot_13")
    raise ValueError(f"неизвестный формат декодирования: {fmt}")

def _codec_autodetect_decode(text):
    stripped = text.strip()
    if re.fullmatch(r"[0-9a-fA-F\s]+", stripped) and len(stripped.replace(" ", "")) % 2 == 0 and len(stripped) >= 4:
        try:
            return "hex", _codec_decode("hex", stripped)
        except Exception:
            pass
    if re.fullmatch(r"[A-Za-z0-9+/=\s]+", stripped) and len(stripped.replace(" ", "").replace("=", "")) % 4 != 1:
        try:
            return "base64", _codec_decode("base64", stripped)
        except Exception:
            pass
    if "%" in stripped:
        try:
            return "url", _codec_decode("url", stripped)
        except Exception:
            pass
    return "rot13", _codec_decode("rot13", stripped)

def tool_text_codec(request_obj, body, args=None):
    args = args or {}
    op = (args.get("op") or "").strip().lower()
    fmt = (args.get("format") or "").strip().lower() or None
    text = args.get("text", "") or ""

    if op == "encode":
        if not fmt:
            return "ошибка: для encode нужно указать format (base64/url/hex/rot13)"
        try:
            result = _codec_encode(fmt, text)
        except Exception as e:
            return f"ошибка кодирования: {e}"
        return f"результат encode/{fmt}: {result}"

    if op == "decode":
        try:
            if fmt:
                result = _codec_decode(fmt, text)
                used_fmt = fmt
            else:
                used_fmt, result = _codec_autodetect_decode(text)
        except Exception as e:
            return f"ошибка декодирования: {e}"
        return f"результат decode/{used_fmt}: {result}"

    if op == "find":
        query = args.get("query", "") or ""
        if not query:
            return "ошибка: для find нужно поле query (что искать)"
        if not text:
            return "ошибка: для find нужно поле text (где искать)"
        case_sensitive = bool(args.get("case_sensitive", False))
        haystack = text if case_sensitive else text.lower()
        needle = query if case_sensitive else query.lower()
        positions = []
        start = 0
        while True:
            idx = haystack.find(needle, start)
            if idx == -1:
                break
            positions.append(idx)
            start = idx + 1
        if not positions:
            return f"по запросу «{query}» ничего не найдено в тексте (длина текста: {len(text)} симв.)"
        snippets = []
        for pos in positions[:5]:
            ctx_start = max(0, pos - 20)
            ctx_end = min(len(text), pos + len(query) + 20)
            snippet = text[ctx_start:ctx_end].replace("\n", " ")
            snippets.append(f"...{snippet}...")
        more = f" (и ещё {len(positions) - 5})" if len(positions) > 5 else ""
        return (
            f"найдено {len(positions)} вхожден{'ие' if len(positions)==1 else 'ий'} «{query}»{more}. "
            f"Позиции (посимвольно): {positions[:10]}. Контекст: " + " | ".join(snippets)
        )

    return f"ошибка: неизвестная операция '{op}' (ожидается encode/decode/find)"

AVAILABLE_TOOLS = {
    "get_time": {
        "fn": tool_get_time,
        "description": "текущая дата и точное время (UTC и, если известно, локальное время/часовой пояс пользователя)",
        "full_description": (
            "Возвращает текущую дату и точное время. Аргументов не принимает. "
            "Всегда отдаёт время в UTC; если фронтенд передал client_time/client_timezone, "
            "дополнительно возвращает локальное время и часовой пояс пользователя. "
            "Используй, когда нужно точно знать, какой сейчас день/время (например, чтобы "
            "посчитать, сколько дней осталось до даты, или подписать документ актуальной датой) — "
            "не полагайся на дату из своих знаний, она может быть устаревшей. "
            "Вызов без аргументов: call_tool: get_time"
        ),
    },
    "get_user_ip": {
        "fn": tool_get_user_ip,
        "description": "IP-адрес пользователя, с которого пришёл запрос",
        "full_description": (
            "Возвращает IP-адрес пользователя, определённый сервером по заголовкам запроса "
            "(X-Forwarded-For, затем X-Real-IP, затем адрес соединения). Аргументов не принимает. "
            "Используй только если пользователь явно спрашивает свой IP или это нужно для задачи, "
            "которую он попросил решить (например, диагностика сети). Не используй для профилирования "
            "или сбора данных о пользователе без явного запроса. "
            "Вызов без аргументов: call_tool: get_user_ip"
        ),
    },
    "connect_telegram_bot": {
        "fn": tool_connect_telegram_bot,
        "description": (
            "показать пользователю окно для ввода токена Telegram-бота, чтобы дальше общаться "
            "с этой же моделью прямо в Telegram. Вызывай, когда пользователь явно просит подключить "
            "бота / общаться через Telegram / даёт понять, что хочет писать боту вместо сайта. "
            "Аргументов не нужно: call_tool: connect_telegram_bot"
        ),
        "full_description": (
            "Открывает на экране пользователя модальное окно для ввода токена Telegram-бота "
            "(токен от @BotFather). Сам инструмент ничего не подключает — он только просит фронтенд "
            "показать это окно. Дальше пользователь сам вставляет токен в открывшееся окно, и уже "
            "фронтенд отдельно обращается к серверному эндпоинту подключения — от тебя больше ничего "
            "не требуется после вызова инструмента. "
            "Вызывай только когда пользователь явно просит подключить/настроить Telegram-бота или "
            "прямо говорит, что хочет общаться с этой моделью через Telegram вместо сайта. "
            "Не вызывай просто потому, что разговор коснулся Telegram или ботов в общем смысле. "
            "Аргументов не нужно: call_tool: connect_telegram_bot"
        ),
    },
    "connect_discord_bot": {
        "fn": tool_connect_discord_bot,
        "description": (
            "показать пользователю окно для ввода токена Discord-бота, чтобы дальше общаться "
            "с этой же моделью в личных сообщениях (DM) Discord. Вызывай, когда пользователь явно "
            "просит подключить бота / общаться через Discord. "
            "Аргументов не нужно: call_tool: connect_discord_bot"
        ),
        "full_description": (
            "Открывает на экране пользователя модальное окно для ввода токена Discord-бота. "
            "Так же, как и с Telegram — сам инструмент ничего не подключает, а лишь просит "
            "фронтенд показать окно ввода токена; подключение бота происходит на сервере уже "
            "после того, как пользователь сам вставит токен. После общения бот будет отвечать "
            "той же моделью в личных сообщениях (DM) Discord. "
            "Вызывай только по явной просьбе пользователя подключить Discord-бота, а не при "
            "простом упоминании Discord. "
            "Аргументов не нужно: call_tool: connect_discord_bot"
        ),
    },
    "text_codec": {
        "fn": tool_text_codec,
        "description": (
            "быстрое кодирование/декодирование текста (base64/url/hex/rot13) и поиск подстроки внутри "
            "текста — один инструмент на все три задачи. Аргументы передаются как JSON сразу после имени "
            "на той же строке, например: "
            'call_tool: text_codec {"op": "decode", "text": "SGVsbG8="} или '
            'call_tool: text_codec {"op": "encode", "format": "base64", "text": "привет"} или '
            'call_tool: text_codec {"op": "find", "text": "текст где искать", "query": "что искать"}. '
            "Если format не указан для decode — сервер сам пытается угадать формат."
        ),
        "has_args": True,
        "full_description": (
            "Универсальный инструмент для трёх операций с текстом: кодирование, декодирование и поиск "
            "подстроки. Используй его вместо того, чтобы вычислять base64/hex/url/rot13 в уме — сервер "
            "сделает это точно и быстро.\n"
            "Параметры (передаются JSON-объектом сразу после имени инструмента, в одной строке):\n"
            "  - op (обязателен): \"encode\" | \"decode\" | \"find\"\n"
            "  - format: \"base64\" | \"url\" | \"hex\" | \"rot13\" — обязателен для encode; "
            "для decode необязателен (если не указан, сервер сам определит формат по содержимому текста)\n"
            "  - text: сам текст, с которым работаем (для encode/decode — что кодировать/декодировать, "
            "для find — где искать)\n"
            "  - query: подстрока для поиска (только для op=find)\n"
            "  - case_sensitive: true/false — учитывать регистр при поиске (только для op=find, "
            "по умолчанию false)\n"
            "Примеры:\n"
            '  call_tool: text_codec {"op": "decode", "text": "SGVsbG8="}\n'
            '  call_tool: text_codec {"op": "encode", "format": "base64", "text": "привет"}\n'
            '  call_tool: text_codec {"op": "find", "text": "текст где искать", "query": "что искать"}\n'
            "Результат find включает количество совпадений, их позиции (посимвольно) и контекст вокруг "
            "каждого найденного места."
        ),
    },
    "web_fetch": {
        "fn": tool_web_fetch,
        "description": (
            "открыть страницу по прямой ссылке (http/https) и получить её текстовое содержимое "
            "(HTML-теги вырезаются). Не для скачивания файлов — только чтение текста/HTML/JSON. "
            f"Аргумент: {{\"url\": \"https://...\"}}. Таймаут {WEB_TOOL_TIMEOUT_SECONDS} сек, "
            f"лимит {WEB_DOWNLOAD_MAX_BYTES // (1024*1024)} МБ. "
            'Вызов: call_tool: web_fetch {"url": "https://example.com/page"}'
        ),
        "has_args": True,
        "full_description": (
            "Скачивает страницу по указанному URL и возвращает её содержимое как чистый текст "
            "(если это HTML — теги, скрипты и стили вырезаются, остаётся только читаемый текст; "
            "если JSON/XML/plain text — возвращается как есть). Используй, когда пользователь дал "
            "ссылку и просит пересказать/проанализировать/найти что-то на странице, или когда тебе "
            "самому нужно прочитать конкретную известную страницу.\n"
            "Параметры (JSON сразу после имени):\n"
            "  - url (обязателен): полная ссылка, начинающаяся с http:// или https://\n"
            f"Ограничения: таймаут {WEB_TOOL_TIMEOUT_SECONDS} секунд, максимум "
            f"{WEB_DOWNLOAD_MAX_BYTES // (1024*1024)} МБ скачиваемых данных, текст в ответе обрезается "
            f"до {WEB_FETCH_MAX_CHARS} символов (если страница больше — придёт начало с пометкой "
            "об обрезке). Локальные и внутренние адреса (localhost, 127.0.0.1, 192.168.x.x, 10.x.x.x "
            "и т.п.) заблокированы политикой безопасности.\n"
            "Если по ссылке лежит не текст/HTML (например, картинка, архив, PDF как бинарник), "
            "инструмент сообщит об этом — в таком случае используй call_tool: web_download.\n"
            'Вызов: call_tool: web_fetch {"url": "https://example.com/page"}'
        ),
    },
    "web_download": {
        "fn": tool_web_download,
        "description": (
            "скачать файл по прямой ссылке (http/https) и приложить его к ответу как вложение. "
            f"Аргументы: {{\"url\": \"https://...\", \"filename\": \"необязательно.расш\"}}. "
            f"Лимит {WEB_DOWNLOAD_MAX_BYTES // (1024*1024)} МБ, таймаут {WEB_TOOL_TIMEOUT_SECONDS} сек. "
            'Вызов: call_tool: web_download {"url": "https://example.com/file.pdf"}'
        ),
        "has_args": True,
        "returns_files": True,
        "full_description": (
            "Скачивает файл с указанного URL целиком (в бинарном виде) и прикладывает его к ответу "
            "как готовое вложение, которое пользователь сможет открыть или скачать — так же, как "
            "если бы файл был отправлен через send_file, но с реальным содержимым из интернета "
            "(картинки, PDF, архивы, документы и т.п. — что угодно, не только текст).\n"
            "Параметры (JSON сразу после имени):\n"
            "  - url (обязателен): полная прямая ссылка на файл, начинающаяся с http:// или https://\n"
            "  - filename (необязателен): как назвать файл; если не указан, имя берётся из URL, "
            "а расширение — при необходимости угадывается по Content-Type ответа\n"
            f"Ограничения: таймаут {WEB_TOOL_TIMEOUT_SECONDS} секунд, максимальный размер файла "
            f"{WEB_DOWNLOAD_MAX_BYTES // (1024*1024)} МБ (если сервер заранее сообщает больший размер "
            "или скачивание превышает лимит — инструмент останавливается с ошибкой, ничего не сохраняя). "
            "Локальные и внутренние адреса заблокированы той же политикой безопасности, что и у "
            "web_fetch.\n"
            "После успешного вызова просто сообщи пользователю, что файл готов — не нужно вставлять "
            "его содержимое в текст ответа и не нужно дополнительно вызывать send_file.\n"
            'Вызов: call_tool: web_download {"url": "https://example.com/file.pdf"}'
        ),
    },
}

if ffmpeg_available():
    AVAILABLE_TOOLS["video_edit"] = {
        "fn": tool_video_edit,
        "description": VIDEO_TOOL_DESCRIPTION,
        "has_args": True,
        "full_description": VIDEO_TOOL_DESCRIPTION,
    }

def _call_agent_model(agent, user_message):
    provider = (agent.get("provider") or "").strip()
    model = (agent.get("model") or "").strip()
    system_prompt = agent.get("prompt") or ""
    own_key = (agent.get("api_key") or "").strip()

    if not model:
        return "ошибка: у этого агента не указана модель в настройках"

    from p13_routing import call_with_key_rotation, guess_provider_and_call

    messages = [{"role": "user", "content": user_message}]

    try:
        if own_key:

            if not provider or provider not in PROVIDERS:
                return f"ошибка: у агента указан собственный ключ, но провайдер '{provider}' неизвестен серверу"
            fn = PROVIDERS[provider]
            reply, _files = fn(model, messages, system_prompt, 1.0, 2048, own_key, [], False, False)
        elif provider and provider in PROVIDERS:
            reply, _files = call_with_key_rotation(
                provider, model, messages, system_prompt, 1.0, 2048, [], False, False
            )
        else:

            reply, _files, _used = guess_provider_and_call(
                model, messages, system_prompt, 1.0, 2048, [],
                hinted_provider=provider or None, code_execution=False, web_search=False,
            )
    except ProviderError as e:
        return f"ошибка агента: {e}"
    except Exception as e:
        return f"ошибка агента: {e}"

    clean_reply, _ = extract_tool_calls(reply)
    return (clean_reply or reply).strip()

def _make_agent_tool_fn(slot):
    def _tool_fn(request_obj, body, args=None):
        agent = _agent_by_slot(slot)
        if agent is None or not agent.get("enabled"):
            return "ошибка: этот агент выключен или не настроен"
        args = args or {}
        message = (args.get("message") or "").strip()
        if not message:
            return "ошибка: для обращения к агенту нужно поле message с текстом задачи/вопроса"
        return _call_agent_model(agent, message)
    return _tool_fn

def _refresh_agent_tools():
    global TOOLS_SYSTEM_PROMPT_ADDON, TOOLS_SYSTEM_PROMPT_ADDON_FULL

    for slot in range(1, MAX_AGENTS + 1):
        AVAILABLE_TOOLS.pop(f"ask_agent_{slot}", None)

    for agent in _agents_store["agents"]:
        if not agent.get("enabled"):
            continue
        slot = agent["slot"]
        name = (agent.get("name") or f"Агент {slot}").strip()
        role_hint = (agent.get("prompt") or "").strip().replace("\n", " ")
        role_hint_short = role_hint[:160] + "…" if len(role_hint) > 160 else role_hint
        description = (
            f'обратиться за помощью к суб-агенту "{name}" — специализированному помощнику '
            f'(роль/промт: {role_hint_short or "не задан"}). Используй, когда задача явно подходит под '
            f'его специализацию. Аргумент — JSON с полем message: '
            f'call_tool: ask_agent_{slot} {{"message": "конкретный вопрос или задача для агента"}}'
        )
        full_description = (
            f'Передаёт задачу отдельному суб-агенту "{name}" (слот {slot}) — у него своя модель, '
            f'провайдер и системный промпт, настроенные независимо от основного диалога. '
            f'Полный промпт/роль этого агента: {role_hint or "не задан"}.\n'
            f'Используй этот инструмент, когда часть задачи явно подходит под специализацию агента '
            f'и есть смысл делегировать её, а не решать самому. Агент не видит историю текущего диалога — '
            f'передавай в message самодостаточный, конкретный вопрос или задачу, со всем нужным контекстом.\n'
            f'Аргумент — JSON-объект с единственным полем message (строка):\n'
            f'  call_tool: ask_agent_{slot} {{"message": "конкретный вопрос или задача для агента"}}\n'
            f'Результат вернётся текстом в следующем сообщении как "ask_agent_{slot}: <ответ агента>".'
        )
        AVAILABLE_TOOLS[f"ask_agent_{slot}"] = {
            "fn": _make_agent_tool_fn(slot),
            "description": description,
            "full_description": full_description,
            "has_args": True,
        }

    TOOLS_SYSTEM_PROMPT_ADDON = _tools_system_prompt_addon(full=False)
    TOOLS_SYSTEM_PROMPT_ADDON_FULL = _tools_system_prompt_addon(full=True)

def _tools_system_prompt_addon(full=False):
    if not full:
        lines = ["\n\nИнструменты (вызывай строкой, если реально нужно):"]
        for name, meta in AVAILABLE_TOOLS.items():
            lines.append(f"call_tool: {name} — {meta['description']}")
        lines.append(
            "Формат: отдельная строка, без ```; JSON-аргументы сразу после имени."
        )
        return "\n".join(lines)

    lines = [
        "\n\n=== Инструменты (подробное описание) ===",
        "Ты можешь вызывать инструменты сервера, написав отдельной строкой в своём ответе:",
        "call_tool: имя_инструмента",
        "или, если инструмент принимает аргументы, JSON сразу после имени на той же строке:",
        'call_tool: имя_инструмента {"параметр": "значение"}',
        "",
        "Правила вызова инструментов:",
        "1. Вызывай инструмент, только если он реально нужен для ответа — не вызывай \"на всякий случай\".",
        "2. Строка call_tool: должна быть отдельной строкой, без markdown-форматирования и без ``` вокруг неё.",
        "3. JSON-аргументы, если они есть, идут в той же строке сразу после имени инструмента, валидным JSON.",
        "4. После вызова инструмента результат придёт отдельным сообщением "
        "\"[Результаты запрошенных инструментов]\" — используй его в следующем ответе, "
        "не выдумывай результат заранее.",
        "5. За один свой ответ можно вызвать несколько инструментов — каждый на отдельной строке.",
        "6. Не сообщай пользователю технические детали вызова (имя инструмента, сырой JSON) — "
        "используй результат, чтобы естественно ответить по существу.",
        "",
        "Доступные инструменты:",
    ]
    for name, meta in AVAILABLE_TOOLS.items():
        full_desc = meta.get("full_description") or meta["description"]
        lines.append(f"\n--- {name} ---")
        lines.append(full_desc)
    return "\n".join(lines)

TOOLS_SYSTEM_PROMPT_ADDON = ""
TOOLS_SYSTEM_PROMPT_ADDON_FULL = ""
_refresh_agent_tools()

def extract_tool_calls(text):
    if not text or "call_tool:" not in text:
        return text, []

    calls = []

    for name, raw_args in TOOL_CALL_WITH_ARGS_RE.findall(text):
        if name not in AVAILABLE_TOOLS:
            continue
        try:
            args = json.loads(raw_args)
        except Exception:
            args = None
        calls.append((name, args))
    clean_text = TOOL_CALL_WITH_ARGS_RE.sub("", text)

    already = {n for n, _ in calls}
    for name in TOOL_CALL_RE.findall(clean_text):
        if name in AVAILABLE_TOOLS and name not in already:
            calls.append((name, None))
    clean_text = TOOL_CALL_RE.sub("", clean_text).strip()

    return clean_text, calls

def run_tools(calls, request_obj, body):
    results = []
    files = []
    for name, args in calls:
        meta = AVAILABLE_TOOLS.get(name)
        if not meta:

            results.append(f"{name}: ошибка при выполнении инструмента: неизвестный инструмент '{name}'")
            continue
        try:
            if meta.get("has_args"):
                value = meta["fn"](request_obj, body, args)
            else:
                value = meta["fn"](request_obj, body)
            if meta.get("returns_files"):
                # Инструменты с returns_files=True возвращают кортеж (текст, [файлы]).
                value, tool_files = value
                if tool_files:
                    files.extend(tool_files)
        except Exception as e:
            value = f"ошибка при выполнении инструмента: {e}"
        results.append(f"{name}: {value}")
    result_text = "[Результаты запрошенных инструментов]\n" + "\n".join(results)
    return result_text, files
