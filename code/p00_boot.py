# -*- coding: utf-8 -*-
"""
p00_boot — заставка запуска и проверка библиотек.

ТОЛЬКО стандартная библиотека Python: модуль работает, даже если flask ещё не установлен.

Как это устроено:
  1. main.py первым делом зовёт start(): на порту сервера поднимается крошечный HTTP-сервер,
     который отдаёт config/splash.html (молния -> сине-зелёный текст -> статус -> 3 2 1).
  2. Пока страница играет, start() проверяет библиотеки. Не хватает — ставит их через pip
     (страница пишет «Скачиваем библиотеки…»), хватает — «Всё готово».
  3. main.py спокойно импортирует приложение (Flask уже есть), потом зовёт handoff():
     он ждёт, пока страница дойдёт до конца отсчёта, закрывает заставку и освобождает порт.
     Дальше на этом же порту стартует настоящий Flask, а страница сама перекидывает на сайт.

Если браузер не подключился (сервер без экрана, вкладку закрыли) — заставка пропускается
и Flask стартует сразу. Совсем отключить: SPLASH=0. Сколько ждать браузер: SPLASH_WAIT=15.
"""
import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# (имя для import, имя для pip). Добавил зависимость в requirements.txt — добавь и сюда.
# psutil сюда не входит намеренно: он необязателен (p17_local_models умеет без него).
REQUIRED = [
    ("flask", "flask"),
    ("flask_cors", "flask-cors"),
    ("requests", "requests"),
]

# В подпись под статусом попадают только строки о ходе установки (не ошибки и не подсказки pip).
_PROGRESS_RE = re.compile(r"^(Collecting|Downloading|Installing|Using cached|Processing|Building|Preparing|"
                          r"Obtaining|Requirement already|Successfully)", re.I)

_state = {"phase": "checking", "detail": "", "error": "", "packages": []}
_state_lock = threading.Lock()

_server = None
_html = b""
_client_seen = threading.Event()   # страница заставки хоть раз обратилась к серверу
_handoff = threading.Event()       # страница досчитала до 1 и просит отдать порт
_last_seen = 0.0                   # когда страница обращалась в последний раз
_browser_opened = False
_started = False                   # start() уже отработал (его могут позвать и лаунчер, и main.py)
_url = ""


# ------------------------------------------------------------------ консоль

def _use_color():
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    return os.name != "nt" or bool(os.environ.get("WT_SESSION"))


def _log(msg):
    tag = "[FluxerAi]"
    if _use_color():
        tag = "\x1b[38;5;45m[Fluxer\x1b[38;5;49mAi]\x1b[0m"
    print(f"{tag} {msg}", flush=True)


def _set(**kw):
    with _state_lock:
        _state.update(kw)


# ------------------------------------------------------------------ библиотеки

def _missing():
    """Список (import_name, pip_name) для библиотек, которых не хватает."""
    out = []
    for mod, pip_name in REQUIRED:
        try:
            found = importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            out.append((mod, pip_name))
    return out


def _run_pip(cmd):
    """Запускает pip, транслирует вывод в консоль и в статус заставки. -> (ok, хвост_вывода)."""
    tail = []
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except OSError as e:
        return False, [str(e)]
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        print("    " + line, flush=True)
        tail.append(line)
        del tail[:-25]
        if _PROGRESS_RE.match(line.strip()):
            _set(detail=line.strip()[:90])
    return proc.wait() == 0, tail


def _pip_install(pip_names):
    base = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    ok, tail = _run_pip(base + list(pip_names))
    # Debian/Ubuntu (PEP 668) без флага отказывается ставить в системный python — повторяем с ним.
    if not ok and any("externally-managed-environment" in ln or "externally managed" in ln for ln in tail):
        _log("Система просит флаг --break-system-packages — повторяю с ним")
        ok, tail = _run_pip(base + ["--break-system-packages"] + list(pip_names))
    return ok, tail


# ------------------------------------------------------------------ сервер заставки

class _Handler(BaseHTTPRequestHandler):
    server_version = "FluxerBoot"

    def log_message(self, *args):   # тишина в консоли
        pass

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", head=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Fluxer-Boot", "1")     # по нему страница отличает заставку от настоящего сайта
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _route(self, head):
        global _last_seen
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            _last_seen = time.time()
            _client_seen.set()
            self._send(200, _html, "text/html; charset=utf-8", head)
        elif path == "/__boot/status":
            _last_seen = time.time()
            _client_seen.set()
            with _state_lock:
                data = json.dumps(_state, ensure_ascii=False).encode("utf-8")
            self._send(200, data, "application/json; charset=utf-8", head)
        else:
            self._send(503, b'{"error": "starting"}', "application/json; charset=utf-8", head)

    def do_GET(self):
        self._route(False)

    def do_HEAD(self):
        self._route(True)

    def do_POST(self):
        if self.path.split("?", 1)[0] == "/__boot/handoff":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(min(n, 1024))
            except (ValueError, OSError):
                pass
            _handoff.set()
            self._send(200, b'{"ok": true}', "application/json; charset=utf-8")
        else:
            self._send(404, b'{"error": "not found"}', "application/json; charset=utf-8")


def _stop_server():
    global _server
    srv, _server = _server, None
    if srv is not None:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass


def _install_excepthook():
    """Если приложение упало при импорте — показать ошибку в заставке, а не оставить её висеть."""
    prev = sys.excepthook

    def hook(exc_type, exc, tb):
        prev(exc_type, exc, tb)                       # сначала обычный traceback в консоль
        if _server is not None and not issubclass(exc_type, KeyboardInterrupt):
            _set(phase="error", error=f"{exc_type.__name__}: {exc}"[:300])
            time.sleep(4)                             # даём странице успеть показать
    sys.excepthook = hook


# ------------------------------------------------------------------ публичный API

def browser_opened():
    return _browser_opened


def _is_termux():
    return bool(os.environ.get("TERMUX_VERSION")) or "com.termux" in os.environ.get("PREFIX", "")


def _try_run(cmd):
    """Запускает команду открытия ссылки. True — если она отработала без ошибки."""
    exe = shutil.which(cmd[0])
    if not exe:
        return False
    try:
        r = subprocess.run([exe] + cmd[1:], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def _open_url(url):
    if _is_termux():
        # termux-open-url есть в стандартной установке Termux; `am start` — запасной путь
        if _try_run(["termux-open-url", url]):
            return
        if _try_run(["am", "start", "--user", "0", "-a", "android.intent.action.VIEW", "-d", url]):
            return
    elif os.name == "nt":
        try:
            os.startfile(url)   # noqa: открывает браузер по умолчанию
            return
        except Exception:
            pass
    elif sys.platform == "darwin":
        if _try_run(["open", url]):
            return
    try:
        if webbrowser.open(url):
            return
    except Exception:
        pass
    if os.name == "posix":
        _try_run(["xdg-open", url])


def open_browser(url, delay=0.0):
    """Открывает url в браузере (Termux / Windows / macOS / Linux)."""
    global _browser_opened
    _browser_opened = True

    def run():
        if delay:
            time.sleep(delay)
        _open_url(url)

    threading.Thread(target=run, daemon=True).start()


def start(host="0.0.0.0", port=5000, splash=True, splash_file="", open_browser_flag=False):
    """Вызывать самой первой строкой main.py, до импорта Flask и остального кода."""
    global _server, _html, _started, _url

    # Дочерний процесс автоперезапуска Flask (debug): библиотеки уже проверены, заставка не нужна.
    if os.environ.get("WERKZEUG_RUN_MAIN"):
        return
    # Одиночный main.py уже вызвал start() в самом начале — второй раз не нужен.
    if _started:
        return
    _started = True

    url = f"http://localhost:{port}"
    _url = url

    if splash and splash_file and os.path.isfile(splash_file):
        try:
            with open(splash_file, "rb") as f:
                _html = f.read()
            srv = ThreadingHTTPServer((host, port), _Handler)
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
            _server = srv
            _install_excepthook()
            _log(f"Заставка: {url}")
            if open_browser_flag:
                open_browser(url)
        except OSError as e:
            _log(f"Заставка отключена ({e}). Продолжаю без неё.")
            _server = None

    missing = _missing()
    if missing:
        pip_names = [p for _, p in missing]
        _set(phase="installing", packages=pip_names, detail=", ".join(pip_names))
        _log("Скачиваем библиотеки: " + ", ".join(pip_names))
        ok, tail = _pip_install(pip_names)
        importlib.invalidate_caches()
        still = _missing()
        if not ok or still:
            names = " ".join(p for _, p in (still or missing))
            msg = f"Не удалось скачать: {names}. Проверь интернет и выполни: pip install {names}"
            _set(phase="error", error=msg)
            _log(msg)
            if tail:
                _log("Последние строки pip: " + " | ".join(tail[-3:]))
            if _server is not None and _client_seen.is_set():
                time.sleep(8)                            # даём странице показать ошибку
            sys.exit(1)

    _set(phase="ready", detail="", error="")
    _log("Всё готово")


def handoff(client_wait=15.0):
    """Вызывать прямо перед app.run(): ждёт конец заставки и освобождает порт."""
    if _server is None:
        return

    t0 = time.time()
    if client_wait and not _client_seen.is_set():
        _log(f"Жду браузер: открой {_url} (до {int(client_wait)} с)")
    while not _client_seen.is_set() and time.time() - t0 < client_wait:
        time.sleep(0.1)

    if _client_seen.is_set():
        _log("Играет заставка в браузере…")
        t1 = time.time()
        while not _handoff.is_set():
            # вкладку закрыли/свернули — не ждём; но и вечно ждать не будем
            if time.time() - _last_seen > 6 or time.time() - t1 > 90:
                break
            time.sleep(0.1)
    else:
        _log("Браузер не подключился — заставка пропущена")

    _stop_server()
