import gzip
import hashlib
import os
from flask import Response, jsonify, request, send_from_directory
from p01_app import app
from config.settings import BASE_DIR, DATA_DIR, FRONTEND_FILE

# Страница весит ~400 КБ. Раньше она читалась с диска на каждый запрос и уходила без сжатия.
# Теперь: читаем один раз, пересчитываем только если файл изменился (правки без перезапуска
# по-прежнему работают), отдаём в gzip (~93 КБ) и с ETag, чтобы браузер брал её из кеша.
_page_cache = {"mtime": None, "raw": b"", "gz": b"", "etag": ""}


def _load_page():
    try:
        mtime = os.stat(FRONTEND_FILE).st_mtime_ns
    except OSError:
        mtime = None
    if mtime is not None and mtime == _page_cache["mtime"]:
        return _page_cache
    with open(FRONTEND_FILE, "rb") as f:
        raw = f.read()
    _page_cache.update(
        mtime=mtime,
        raw=raw,
        gz=gzip.compress(raw, compresslevel=6),
        etag='"' + hashlib.md5(raw).hexdigest()[:16] + '"',
    )
    return _page_cache


def _read_frontend():
    return _load_page()["raw"].decode("utf-8")


@app.route("/", methods=["GET"])
def serve_index():
    external = os.path.join(BASE_DIR, "index.html")
    if os.path.isfile(external):
        return send_from_directory(BASE_DIR, "index.html")

    page = _load_page()
    headers = {
        "ETag": page["etag"],
        # Всегда сверяемся с сервером (304, если не менялось) — так правки frontend.html видны сразу.
        "Cache-Control": "no-cache",
        "Vary": "Accept-Encoding",
    }
    if request.headers.get("If-None-Match") == page["etag"]:
        return Response(status=304, headers=headers)

    if "gzip" in (request.headers.get("Accept-Encoding") or "").lower():
        headers["Content-Encoding"] = "gzip"
        return Response(page["gz"], mimetype="text/html", headers=headers)
    return Response(page["raw"], mimetype="text/html", headers=headers)


@app.route("/<path:filename>", methods=["GET"])
def serve_static(filename):
    full_path = os.path.join(BASE_DIR, filename)

    real = os.path.realpath(full_path)
    data_real = os.path.realpath(DATA_DIR)
    if real == data_real or real.startswith(data_real + os.sep):
        return jsonify({"error": "not found"}), 404
    if os.path.isfile(full_path):
        return send_from_directory(BASE_DIR, filename)
    return jsonify({"error": "not found"}), 404
