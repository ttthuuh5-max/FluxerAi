# Монтаж видео через ffmpeg — инструмент для модели (call_tool: video_edit).
#
# Модель НЕ пишет сырую ffmpeg-команду. Вместо этого она выбирает одну из
# готовых операций (op) и передаёт понятные параметры (секунды, текст,
# имена входных вложений и т.д.) — этот модуль сам собирает безопасный
# список аргументов ffmpeg и запускает его через subprocess без shell=True.
#
# Входные файлы модель указывает по ИМЕНИ вложения, которое уже прикреплено
# к текущему сообщению (body["attachments"]) — то есть модель сначала
# "смотрит" видео благодаря обычной поддержке video-вложений у провайдера
# (например Google), а затем тем же вызовом ссылается на этот же файл для
# монтажа. Готовый результат кладётся в body["_video_output_files"] —
# оттуда p14_chat.py забирает его и добавляет в files ответа пользователю.

import base64
import os
import re
import shutil
import subprocess
import tempfile
import uuid

FFMPEG_BIN = shutil.which("ffmpeg")
FFPROBE_BIN = shutil.which("ffprobe")

MAX_INPUT_BYTES = 200 * 1024 * 1024   # 200 МБ на входной файл — монтаж больших видео тут не задача
FFMPEG_TIMEOUT_SECONDS = 300          # 5 минут на операцию, чтобы не подвесить сервер

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def ffmpeg_available():
    return FFMPEG_BIN is not None


def _safe_filename(name, default="file"):
    name = os.path.basename((name or "").strip()) or default
    name = _SAFE_NAME_RE.sub("_", name)
    return name[:120] or default


def _find_attachment(attachments, name):
    if not name:
        return None
    name = name.strip()
    for a in attachments:
        if a.get("name") == name:
            return a
    # мягкое совпадение без учёта регистра — модель иногда не копирует имя буква в букву
    lname = name.lower()
    for a in attachments:
        if (a.get("name") or "").lower() == lname:
            return a
    return None


def _guess_output_mime(ext):
    ext = ext.lower().lstrip(".")
    return {
        "mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm",
        "gif": "image/gif", "mp3": "audio/mpeg", "aac": "audio/aac",
        "wav": "audio/wav", "png": "image/png", "jpg": "image/jpeg",
    }.get(ext, "application/octet-stream")


def _run_ffmpeg(args, cwd, timeout=FFMPEG_TIMEOUT_SECONDS):
    cmd = [FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error"] + args
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffmpeg не уложился в {timeout} сек. и был остановлен")
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace")[-1500:]
        raise RuntimeError(f"ffmpeg завершился с ошибкой: {err.strip() or 'без сообщения'}")


def _write_attachment_to_disk(attachment, dest_path):
    try:
        raw = base64.b64decode(attachment["data_base64"])
    except Exception as e:
        raise RuntimeError(f"не удалось декодировать вложение '{attachment.get('name')}': {e}")
    if len(raw) > MAX_INPUT_BYTES:
        raise RuntimeError(
            f"файл '{attachment.get('name')}' слишком большой для монтажа "
            f"({len(raw)/1024/1024:.1f} МБ, лимит {MAX_INPUT_BYTES/1024/1024:.0f} МБ)"
        )
    with open(dest_path, "wb") as f:
        f.write(raw)


def _read_output_file(path, out_name):
    with open(path, "rb") as f:
        raw = f.read()
    ext = out_name.rsplit(".", 1)[-1] if "." in out_name else "mp4"
    return {
        "name": out_name,
        "mime_type": _guess_output_mime(ext),
        "data_base64": base64.b64encode(raw).decode("ascii"),
    }


# ---------------------------------------------------------------- операции

def _op_trim(work_dir, in_path, args):
    start = str(args.get("start", "0"))
    duration = args.get("duration")
    end = args.get("end")
    ffargs = ["-i", in_path, "-ss", start]
    if duration is not None:
        ffargs += ["-t", str(duration)]
    elif end is not None:
        ffargs += ["-to", str(end)]
    ffargs += ["-c", "copy"]
    return ffargs


def _op_concat(work_dir, in_paths, args):
    list_path = os.path.join(work_dir, "concat_list.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for p in in_paths:
            f.write(f"file '{os.path.basename(p)}'\n")
    return ["-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy"]


def _op_watermark_text(work_dir, in_path, args):
    text = str(args.get("text", "")).replace("'", "\u2019").replace(":", "\\:")
    position = (args.get("position") or "bottom_right").strip().lower()
    pos_map = {
        "top_left": "x=20:y=20",
        "top_right": "x=w-tw-20:y=20",
        "bottom_left": "x=20:y=h-th-20",
        "bottom_right": "x=w-tw-20:y=h-th-20",
        "center": "x=(w-tw)/2:y=(h-th)/2",
    }
    pos = pos_map.get(position, pos_map["bottom_right"])
    fontsize = int(args.get("font_size", 28))
    vf = f"drawtext=text='{text}':{pos}:fontsize={fontsize}:fontcolor=white:box=1:boxcolor=black@0.4:boxborderw=6"
    return ["-i", in_path, "-vf", vf, "-codec:a", "copy"]


def _op_compress(work_dir, in_path, args):
    crf = int(args.get("crf", 28))
    crf = max(18, min(35, crf))
    preset = (args.get("preset") or "medium").strip().lower()
    if preset not in ("ultrafast", "fast", "medium", "slow"):
        preset = "medium"
    return ["-i", in_path, "-vcodec", "libx264", "-crf", str(crf), "-preset", preset, "-acodec", "aac", "-b:a", "128k"]


def _op_extract_audio(work_dir, in_path, args):
    return ["-i", in_path, "-vn", "-acodec", "libmp3lame", "-q:a", "2"]


def _op_to_gif(work_dir, in_path, args):
    fps = int(args.get("fps", 10))
    width = int(args.get("width", 480))
    start = str(args.get("start", "0"))
    duration = args.get("duration", 5)
    return [
        "-ss", start, "-t", str(duration), "-i", in_path,
        "-vf", f"fps={fps},scale={width}:-1:flags=lanczos",
    ]


def _op_resize(work_dir, in_path, args):
    width = int(args.get("width", -2))
    height = int(args.get("height", -2))
    return ["-i", in_path, "-vf", f"scale={width}:{height}", "-c:a", "copy"]


OPS = {
    "trim": {"fn": _op_trim, "default_ext": "mp4",
             "desc": "обрезать видео по времени: start (сек или ЧЧ:ММ:СС), и duration ИЛИ end"},
    "concat": {"fn": _op_concat, "default_ext": "mp4", "multi_input": True,
               "desc": "склеить несколько видео подряд (нужны одинаковые кодек/разрешение)"},
    "watermark_text": {"fn": _op_watermark_text, "default_ext": "mp4",
                        "desc": "наложить текстовую надпись: text, position (top_left/top_right/bottom_left/bottom_right/center), font_size"},
    "compress": {"fn": _op_compress, "default_ext": "mp4",
                 "desc": "сжать видео: crf (18-35, больше=меньше размер), preset (ultrafast/fast/medium/slow)"},
    "extract_audio": {"fn": _op_extract_audio, "default_ext": "mp3",
                       "desc": "извлечь звуковую дорожку из видео в mp3"},
    "to_gif": {"fn": _op_to_gif, "default_ext": "gif",
               "desc": "сделать gif из фрагмента видео: start, duration (сек), fps, width"},
    "resize": {"fn": _op_resize, "default_ext": "mp4",
               "desc": "изменить размер кадра: width, height (-2 у одной из сторон — сохранить пропорции)"},
}


def tool_video_edit(request_obj, body, args=None):
    if not ffmpeg_available():
        return ("ошибка: на сервере не установлен ffmpeg, монтаж видео недоступен. "
                "Установите ffmpeg и добавьте его в PATH, затем перезапустите сервер.")

    args = args or {}
    op = (args.get("op") or "").strip().lower()
    op_meta = OPS.get(op)
    if not op_meta:
        return (f"ошибка: неизвестная операция '{op}'. Доступные: "
                + ", ".join(f"{k} ({v['desc']})" for k, v in OPS.items()))

    attachments = body.get("attachments") or []
    # normalize уже прошли в p14_chat до вызова run_tools, но подстрахуемся на случай "сырых" данных
    attachments = [a for a in attachments if isinstance(a, dict) and a.get("data_base64")]

    input_names = args.get("input_files")
    if not input_names and args.get("input_file"):
        input_names = [args["input_file"]]
    if not input_names:
        video_atts = [a for a in attachments if (a.get("kind") == "video") or (a.get("mime_type", "").startswith("video/"))]
        if len(video_atts) == 1:
            input_names = [video_atts[0]["name"]]
        else:
            return ("ошибка: нужно указать input_files (список имён видео-вложений из этого сообщения) — "
                     f"в сообщении {'нет видео-вложений' if not video_atts else 'несколько видео, уточните какое именно'}")

    if op_meta.get("multi_input"):
        if len(input_names) < 2:
            return "ошибка: для 'concat' нужно минимум 2 файла в input_files"
    else:
        input_names = input_names[:1]

    resolved = []
    for n in input_names:
        att = _find_attachment(attachments, n)
        if not att:
            return f"ошибка: вложение с именем '{n}' не найдено среди файлов этого сообщения"
        resolved.append(att)

    out_name = _safe_filename(args.get("output_name") or f"edited.{op_meta['default_ext']}")
    if "." not in out_name:
        out_name += f".{op_meta['default_ext']}"

    work_dir = tempfile.mkdtemp(prefix="fluxerai_ffmpeg_")
    try:
        local_paths = []
        for i, att in enumerate(resolved):
            local_name = _safe_filename(att.get("name") or f"input_{i}")
            local_path = os.path.join(work_dir, f"{i}_{local_name}")
            _write_attachment_to_disk(att, local_path)
            local_paths.append(local_path)

        out_path = os.path.join(work_dir, out_name)

        if op_meta.get("multi_input"):
            ffargs = op_meta["fn"](work_dir, local_paths, args)
        else:
            ffargs = op_meta["fn"](work_dir, local_paths[0], args)
        ffargs = ffargs + [out_path]

        try:
            _run_ffmpeg(ffargs, cwd=work_dir)
        except RuntimeError as e:
            return f"ошибка ffmpeg при операции '{op}': {e}"

        if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
            return f"ошибка: ffmpeg не создал выходной файл для операции '{op}'"

        out_file = _read_output_file(out_path, out_name)
        body.setdefault("_video_output_files", []).append(out_file)

        size_mb = len(out_file["data_base64"]) * 3 / 4 / 1024 / 1024
        return (f"готово: операция '{op}' выполнена, результат '{out_name}' "
                f"({size_mb:.1f} МБ) прикреплён к ответу для скачивания.")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


VIDEO_TOOL_DESCRIPTION = (
    "смонтировать/обработать видео через ffmpeg. Работает с видео-вложениями, "
    "уже прикреплёнными к ЭТОМУ сообщению пользователя (сначала посмотри видео "
    "как обычно, затем вызови этот инструмент для монтажа). Аргументы JSON: "
    'op (обязательно, одна из: ' + ", ".join(OPS.keys()) + '), '
    'input_files (список имён вложений-видео из этого сообщения; если видео одно — можно не указывать), '
    'output_name (необязательно, имя результата с расширением). '
    "Параметры операций: " + "; ".join(f"{k} — {v['desc']}" for k, v in OPS.items()) + ". "
    'Пример: call_tool: video_edit {"op": "trim", "input_files": ["clip.mp4"], "start": "00:00:05", "duration": 10}'
)
