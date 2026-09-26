import os
import re
import shutil
import subprocess
import threading
import requests
import traceback
from flask import jsonify, request
from p01_app import app
from config.settings import DATA_DIR

WORKSPACE_DIR = os.path.abspath(os.environ.get("IDE_WORKSPACE") or os.path.join(DATA_DIR, "workspace"))
os.makedirs(WORKSPACE_DIR, exist_ok=True)

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TREE_ENTRIES = 5000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode", "dist", "build"}

AAS_API = "https://sandbox-as-a-service.com/v1"
AAS_DEFAULT_SIZE = "small"
AAS_DEFAULT_TIMEOUT = 60_000

_local_lock = threading.Lock()

def _aas_headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

def _get_aas_key() -> str | None:

    key = os.environ.get("AAS_API_KEY", "").strip()
    if key:
        return key

    try:
        from p05_keys import get_key_sequence
        keys = get_key_sequence("aas")
        if keys:
            return keys[0]
    except Exception:
        pass
    return None

def _run_local(command: str, cwd: str | None, env_extra: dict, timeout_ms: int):

    try:
        cwd = _safe_path(cwd) if cwd else WORKSPACE_DIR
    except ValueError:
        cwd = WORKSPACE_DIR
    os.makedirs(cwd, exist_ok=True)
    timeout_s = min(timeout_ms / 1000, 300)

    merged_env = {**os.environ, **env_extra}

    try:
        with _local_lock:
            proc = subprocess.run(
                ["bash", "-c", command],
                cwd=cwd,
                env=merged_env,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        return {
            "mode": "local",
            "exit_code": proc.returncode,
            "stdout": proc.stdout[:1_048_576],
            "stderr": proc.stderr[:1_048_576],
            "truncated": len(proc.stdout) > 1_048_576 or len(proc.stderr) > 1_048_576,
        }
    except subprocess.TimeoutExpired:
        return {
            "mode": "local",
            "exit_code": 124,
            "stdout": "",
            "stderr": f"command timed out after {timeout_ms}ms",
            "truncated": False,
        }
    except Exception as exc:
        return {
            "mode": "local",
            "exit_code": 1,
            "stdout": "",
            "stderr": str(exc),
            "truncated": False,
        }

def _run_cloud(sandbox_id: str, key: str, command: str, cwd: str | None, env_extra: dict, timeout_ms: int):
    payload = {
        "command": command,
        "timeout_ms": min(timeout_ms, 600_000),
    }
    if cwd:
        payload["cwd"] = cwd
    if env_extra:
        payload["env"] = {str(k): str(v) for k, v in env_extra.items()}

    resp = requests.post(
        f"{AAS_API}/sandboxes/{sandbox_id}/exec",
        headers=_aas_headers(key),
        json=payload,
        timeout=min(timeout_ms / 1000 + 30, 660),
    )
    if resp.status_code != 200:
        raise RuntimeError(f"AAS exec {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    return {
        "mode": "cloud",
        "sandbox_id": sandbox_id,
        "exit_code": data.get("exit_code", -1),
        "stdout": data.get("stdout", ""),
        "stderr": data.get("stderr", ""),
        "truncated": data.get("truncated", False),
        "duration_ms": data.get("duration_ms"),
        "execution_id": data.get("id"),
    }

def _safe_path(rel: str) -> str:
    rel = (rel or "").replace("\\", "/").strip().lstrip("/")
    full = os.path.abspath(os.path.join(WORKSPACE_DIR, rel))
    real = os.path.realpath(full)
    root = os.path.realpath(WORKSPACE_DIR)
    if real != root and not real.startswith(root + os.sep):
        raise ValueError("Путь выходит за пределы рабочей папки")
    return full

def _rel(full: str) -> str:
    return os.path.relpath(full, WORKSPACE_DIR).replace(os.sep, "/")

def _seed_workspace():
    if any(True for _ in os.scandir(WORKSPACE_DIR)):
        return
    seed = {
        "README.md": "# Мой проект\n\nЭто рабочая папка IDE FluxerAi.\nПопроси агента справа что-нибудь создать.\n",
        "index.html": "<!DOCTYPE html>\n<html lang=\"ru\">\n<head>\n  <meta charset=\"UTF-8\">\n  <title>Мой проект</title>\n  <link rel=\"stylesheet\" href=\"styles.css\">\n</head>\n<body>\n  <h1>Привет, мир!</h1>\n  <script src=\"app.js\"></script>\n</body>\n</html>\n",
        "styles.css": "body {\n  font-family: system-ui, sans-serif;\n  margin: 2rem;\n}\n",
        "app.js": "console.log('Привет из FluxerAi IDE!');\n",
    }
    for name, text in seed.items():
        with open(os.path.join(WORKSPACE_DIR, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

_seed_workspace()

def _walk_tree(base: str, budget: list):
    out = []
    try:
        entries = sorted(os.scandir(base), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
    except OSError:
        return out
    for e in entries:
        if budget[0] <= 0:
            break
        if e.is_symlink():
            continue
        if e.is_dir(follow_symlinks=False):
            if e.name in SKIP_DIRS:
                continue
            budget[0] -= 1
            out.append({"name": e.name, "path": _rel(e.path), "type": "folder",
                        "children": _walk_tree(e.path, budget)})
        else:
            budget[0] -= 1
            out.append({"name": e.name, "path": _rel(e.path), "type": "file", "size": e.stat().st_size})
    return out

@app.route("/api/ide/fs/tree", methods=["GET"])
def ide_fs_tree():
    return jsonify({"root": WORKSPACE_DIR, "tree": _walk_tree(WORKSPACE_DIR, [MAX_TREE_ENTRIES])})

@app.route("/api/ide/fs/file", methods=["GET"])
def ide_fs_read():
    try:
        full = _safe_path(request.args.get("path"))
        if not os.path.isfile(full):
            return jsonify({"error": "Файл не найден"}), 404
        if os.path.getsize(full) > MAX_FILE_BYTES:
            return jsonify({"error": "Файл больше 5 МБ — открыть в редакторе нельзя"}), 413
        with open(full, "rb") as f:
            raw = f.read()
        if b"\x00" in raw[:8192]:
            return jsonify({"error": "Бинарный файл — в редакторе не открывается", "binary": True}), 415
        return jsonify({"path": _rel(full), "content": raw.decode("utf-8", errors="replace")})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

@app.route("/api/ide/fs/file", methods=["PUT"])
def ide_fs_write():
    body = request.get_json(force=True) or {}
    path = (body.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path обязателен"}), 400
    content = body.get("content")
    if not isinstance(content, str):
        return jsonify({"error": "content должен быть строкой"}), 400
    try:
        full = _safe_path(path)
        if os.path.isdir(full):
            return jsonify({"error": "Это папка, а не файл"}), 400
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        return jsonify({"written": _rel(full), "bytes": len(content.encode("utf-8"))})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

@app.route("/api/ide/fs/file", methods=["DELETE"])
def ide_fs_delete():
    try:
        full = _safe_path(request.args.get("path"))
        if os.path.realpath(full) == os.path.realpath(WORKSPACE_DIR):
            return jsonify({"error": "Нельзя удалить корень рабочей папки"}), 400
        if os.path.isdir(full):
            shutil.rmtree(full)
        elif os.path.isfile(full):
            os.remove(full)
        else:
            return jsonify({"error": "Не найдено"}), 404
        return jsonify({"deleted": _rel(full)})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

@app.route("/api/ide/fs/mkdir", methods=["POST"])
def ide_fs_mkdir():
    body = request.get_json(force=True) or {}
    try:
        full = _safe_path(body.get("path"))
        os.makedirs(full, exist_ok=True)
        return jsonify({"created": _rel(full)})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

@app.route("/api/ide/fs/move", methods=["POST"])
def ide_fs_move():
    body = request.get_json(force=True) or {}
    try:
        src = _safe_path(body.get("from"))
        dst = _safe_path(body.get("to"))
        if not os.path.exists(src):
            return jsonify({"error": "Источник не найден"}), 404
        if os.path.exists(dst):
            return jsonify({"error": "Назначение уже существует"}), 409
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        return jsonify({"moved": _rel(dst)})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

_RE_DOWNLOAD = re.compile(r"(^|[\s;&|(])(curl|wget|aria2c|axel|yt-dlp|youtube-dl|gdown)\b|git\s+clone|git\s+pull|git\s+fetch|huggingface-cli\s+download|docker\s+pull|scp\b|rsync\b.*:")
_RE_INSTALL  = re.compile(r"(^|[\s;&|(])(pip3?|pipx|poetry|uv|conda|npm|npx|yarn|pnpm|bun|cargo|go\s+install|gem|composer|apt(-get)?|dnf|yum|pacman|apk|brew|choco|winget|snap|flatpak)\s+(install|add|i|update|upgrade|get)\b|python3?\s+-m\s+pip\s+install")
_RE_NETWORK  = re.compile(r"(^|[\s;&|(])(curl|wget|nc|ncat|netcat|ssh|telnet|ftp|sftp|ping|nmap|socat)\b|python3?\s+-m\s+http\.server|npx\s+serve|flask\s+run|uvicorn|gunicorn")
_RE_DELETE   = re.compile(r"(^|[\s;&|(])(rm|rmdir|shred|unlink)\b|find\b.*-delete|git\s+clean|git\s+reset\s+--hard|truncate\b")
_RE_SUDO     = re.compile(r"(^|[\s;&|(])(sudo|su|doas|chmod\s+-R|chown\s+-R)\b")

_RE_FORBIDDEN = re.compile(r"rm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+(/|~|\$HOME|/\*|--no-preserve-root)(\s|$)|mkfs\b|dd\s+if=.*of=/dev/|:\(\)\s*\{|>\s*/dev/sd|shutdown\b|reboot\b|halt\b|poweroff\b|format\s+[a-zA-Z]:")

def _check_perms(command: str, perms: dict | None):
    if _RE_FORBIDDEN.search(command):
        return "Команда заблокирована: потенциально разрушительная для системы."
    if perms is None:
        return None
    if not perms.get("run"):
        return "Запуск команд выключен в разрешениях агента."
    rules = (
        (_RE_SUDO,     "sudo",     "Права администратора (sudo) не разрешены."),
        (_RE_DELETE,   "delete",   "Удаление файлов через команды не разрешено."),
        (_RE_INSTALL,  "install",  "Установка пакетов не разрешена."),
        (_RE_DOWNLOAD, "download", "Скачивание из интернета не разрешено."),
        (_RE_NETWORK,  "network",  "Сетевой доступ не разрешён."),
    )
    for rx, key, msg in rules:
        if rx.search(command) and not perms.get(key):
            return msg
    return None

@app.route("/api/ide/exec", methods=["POST"])
def ide_exec():
    body = request.get_json(force=True) or {}
    command = (body.get("command") or "").strip()
    if not command:
        return jsonify({"error": "command обязателен"}), 400

    mode = (body.get("mode") or "local").lower()
    cwd = body.get("cwd") or None
    env_extra = body.get("env") or {}
    timeout_ms = max(1000, min(int(body.get("timeout_ms") or AAS_DEFAULT_TIMEOUT), 600_000))

    perms = body.get("perms")
    if perms is not None and not isinstance(perms, dict):
        perms = {}
    denied = _check_perms(command, perms)
    if denied:
        return jsonify({"mode": mode, "exit_code": 126, "stdout": "", "stderr": denied,
                        "denied": True, "truncated": False})

    try:
        if mode == "local":
            result = _run_local(command, cwd, env_extra, timeout_ms)
            return jsonify(result)

        elif mode == "cloud":
            key = _get_aas_key()
            if not key:
                return jsonify({"error": "AAS_API_KEY не найден. Добавь в ключи провайдера 'aas'."}), 401
            sandbox_id = (body.get("sandbox_id") or "").strip()
            if not sandbox_id:
                return jsonify({"error": "sandbox_id обязателен для cloud-режима"}), 400
            result = _run_cloud(sandbox_id, key, command, cwd, env_extra, timeout_ms)
            return jsonify(result)

        else:
            return jsonify({"error": f"Неизвестный mode '{mode}', используй 'local' или 'cloud'"}), 400

    except Exception as exc:
        traceback.print_exc()
        return jsonify({"error": str(exc)}), 500

@app.route("/api/ide/sandbox", methods=["POST"])
def ide_create_sandbox():
    key = _get_aas_key()
    if not key:
        return jsonify({"error": "AAS_API_KEY не найден. Добавь в ключи провайдера 'aas'."}), 401

    body = request.get_json(force=True) or {}
    size = body.get("size") or AAS_DEFAULT_SIZE
    timeout_minutes = int(body.get("timeout_minutes") or 60)

    try:
        resp = requests.post(
            f"{AAS_API}/sandboxes",
            headers=_aas_headers(key),
            json={"size": size, "timeout_minutes": timeout_minutes},
            timeout=90,
        )
        if resp.status_code not in (200, 201):
            return jsonify({"error": f"AAS {resp.status_code}: {resp.text[:300]}"}), resp.status_code
        return jsonify(resp.json())
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"error": str(exc)}), 500

@app.route("/api/ide/sandbox/<sandbox_id>", methods=["DELETE"])
def ide_delete_sandbox(sandbox_id):
    key = _get_aas_key()
    if not key:
        return jsonify({"error": "AAS_API_KEY не найден"}), 401
    try:
        resp = requests.delete(
            f"{AAS_API}/sandboxes/{sandbox_id}",
            headers=_aas_headers(key),
            timeout=30,
        )
        if resp.status_code not in (200, 204):
            return jsonify({"error": f"AAS {resp.status_code}: {resp.text[:200]}"}), resp.status_code
        return jsonify({"deleted": sandbox_id})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

@app.route("/api/ide/sandboxes", methods=["GET"])
def ide_list_sandboxes():
    key = _get_aas_key()
    if not key:
        return jsonify({"sandboxes": [], "error": "AAS_API_KEY не найден"})
    try:
        resp = requests.get(f"{AAS_API}/sandboxes", headers=_aas_headers(key), timeout=15)
        if resp.status_code != 200:
            return jsonify({"sandboxes": [], "error": f"AAS {resp.status_code}"})
        return jsonify(resp.json())
    except Exception as exc:
        return jsonify({"sandboxes": [], "error": str(exc)})

@app.route("/api/ide/sandbox/<sandbox_id>/file", methods=["PUT"])
def ide_write_file(sandbox_id):
    key = _get_aas_key()
    if not key:
        return jsonify({"error": "AAS_API_KEY не найден"}), 401
    body = request.get_json(force=True) or {}
    path = (body.get("path") or "").strip()
    content = body.get("content") or ""
    if not path:
        return jsonify({"error": "path обязателен"}), 400
    try:
        resp = requests.put(
            f"{AAS_API}/sandboxes/{sandbox_id}/files",
            headers={**_aas_headers(key), "Content-Type": "application/octet-stream",
                     "X-File-Path": path},
            data=content.encode("utf-8"),
            timeout=30,
        )
        if resp.status_code not in (200, 201, 204):
            return jsonify({"error": f"AAS {resp.status_code}: {resp.text[:200]}"}), resp.status_code
        return jsonify({"written": path})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

@app.route("/api/ide/sandbox/<sandbox_id>/file", methods=["GET"])
def ide_read_file(sandbox_id):
    key = _get_aas_key()
    if not key:
        return jsonify({"error": "AAS_API_KEY не найден"}), 401
    path = request.args.get("path") or "/workspace"
    try:
        resp = requests.get(
            f"{AAS_API}/sandboxes/{sandbox_id}/files",
            headers=_aas_headers(key),
            params={"path": path},
            timeout=30,
        )
        if resp.status_code != 200:
            return jsonify({"error": f"AAS {resp.status_code}: {resp.text[:200]}"}), resp.status_code
        ct = resp.headers.get("Content-Type", "")
        if "application/json" in ct:
            return jsonify(resp.json())

        return jsonify({"path": path, "content": resp.text})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
