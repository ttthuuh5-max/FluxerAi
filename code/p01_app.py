import gzip

from flask import Flask, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# Сжимаем большие JSON-ответы API (список чатов, магазин промтов, логи): текст жмётся в 5-10 раз.
# Своё минимальное сжатие вместо flask-compress — чтобы не добавлять зависимость.
_GZIP_MIN_BYTES = 1024
_GZIP_TYPES = ("application/json", "text/")


@app.after_request
def _gzip_response(resp):
    try:
        if (resp.direct_passthrough or resp.status_code < 200 or resp.status_code >= 300
                or resp.status_code == 204 or "Content-Encoding" in resp.headers
                or "gzip" not in (request.headers.get("Accept-Encoding") or "").lower()
                or not (resp.mimetype or "").startswith(_GZIP_TYPES)
                # потоковые ответы (SSE и т.п.) не трогаем — иначе они перестанут идти по кускам
                or resp.is_streamed):
            return resp
        data = resp.get_data()
        if len(data) < _GZIP_MIN_BYTES:
            return resp
        resp.set_data(gzip.compress(data, compresslevel=5))
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Content-Length"] = str(len(resp.get_data()))
        resp.headers.add("Vary", "Accept-Encoding")
    except Exception:
        pass  # сжатие — оптимизация: при любой неудаче отдаём ответ как есть
    return resp
