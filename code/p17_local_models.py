import gc
import os
import re
import shutil
import subprocess
import threading
import time
import uuid

import requests
from flask import jsonify
from flask import request

from p01_app import app
from p03_storage import _load_json, _save_json, _storage_lock
from config.settings import LOCAL_MODELS_DIR, LOCAL_MODELS_META_FILE

try:
    from p05_keys import API_KEYS
except Exception:
    API_KEYS = {}

os.makedirs(LOCAL_MODELS_DIR, exist_ok=True)

HF_API_BASE = "https://huggingface.co/api"
DOWNLOAD_CHUNK = 1024 * 1024

_models_store = _load_json(LOCAL_MODELS_META_FILE, {"models": {}})
if not isinstance(_models_store.get("models"), dict):
    _models_store["models"] = {}

_DEFAULT_PARAMS = {
    "n_ctx": 4096,
    # -1 = отдать на видеокарты ВСЕ слои. Если llama-cpp-python собран без GPU,
    # параметр просто игнорируется и модель работает на CPU.
    "n_gpu_layers": -1,
    "n_threads": os.cpu_count() or 4,
    "temperature": 0.7,
    # True = делить модель между ВСЕМИ найденными видеокартами (а не грузить всё в одну).
    "use_all_gpus": True,
    # Доли модели по картам, например [0.5, 0.5]. Пусто/None = автоматически по объёму VRAM.
    "tensor_split": None,
}

def _persist_models():
    _save_json(LOCAL_MODELS_META_FILE, _models_store)

def _migrate_old_params():
    """Раньше по умолчанию было n_gpu_layers=0 (только CPU). Такие сохранённые значения
    переводим на новый дефолт (-1 = все слои на GPU) и включаем использование всех карт.
    Если пользователь сам поставил своё число слоёв (не 0) — не трогаем."""
    changed = False
    for m in _models_store["models"].values():
        p = m.get("params")
        if not isinstance(p, dict):
            continue
        if "use_all_gpus" not in p:
            p["use_all_gpus"] = True
            changed = True
            if int(p.get("n_gpu_layers", 0) or 0) == 0:
                p["n_gpu_layers"] = -1
    if changed:
        _persist_models()

_migrate_old_params()

def _hf_headers():
    token = None
    keys = API_KEYS.get("huggingface") or []
    if keys:
        token = keys[0]
    headers = {"User-Agent": "FluxerAi-LocalModels/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers

_SHARD_RE = re.compile(r"^(?P<base>.+)-(?P<idx>\d{5})-of-(?P<total>\d{5})\.gguf$", re.IGNORECASE)

def _shard_info(filename):
    """('base', idx, total) для файла вида X-00001-of-00008.gguf, иначе None. Работает и с подпапками."""
    folder, _, name = filename.rpartition("/")
    m = _SHARD_RE.match(name)
    if not m:
        return None
    base = (folder + "/" if folder else "") + m.group("base")
    return base, int(m.group("idx")), int(m.group("total"))

def _group_shards(files):
    """Склеивает части многофайловых моделей в одну запись (по первой части).
    Неполные наборы (нет каких-то частей в репозитории) помечаются incomplete."""
    groups, out = {}, []
    for f in files:
        info = _shard_info(f["filename"])
        if not info:
            out.append(f)
            continue
        base, idx, total = info
        g = groups.setdefault((base, total), {"parts": {}})
        g["parts"][idx] = f
    for (base, total), g in groups.items():
        parts = [g["parts"][i] for i in sorted(g["parts"])]
        first = parts[0]
        sizes = [x.get("size_bytes") for x in parts]
        total_size = sum(x for x in sizes if x) if all(sizes) else None
        out.append({
            "filename": first["filename"],
            "size_bytes": total_size,
            "size_gb": _fmt_gb(total_size) if total_size else None,
            "param_count": first.get("param_count"),
            "parts": total,
            "parts_found": len(parts),
            "part_files": [x["filename"] for x in parts],
            "incomplete": len(parts) != total or 1 not in g["parts"],
        })
    return out

def _hf_has_key():
    """True, если в ключах провайдера huggingface есть хотя бы один токен."""
    keys = API_KEYS.get("huggingface") or []
    return any((k or "").strip() for k in keys)

def _safe_id(repo_id, filename):
    raw = f"{repo_id}__{filename}"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", raw)

def _fmt_gb(num_bytes):
    if not num_bytes:
        return 0.0
    return round(num_bytes / (1024 ** 3), 2)

def _ram_info():
    try:
        import psutil
        vm = psutil.virtual_memory()
        return vm.total, vm.available
    except Exception:
        pass

    try:
        if os.name == "posix" and os.path.exists("/proc/meminfo"):
            info = {}
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        key = parts[0].strip()
                        val = parts[1].strip().split()[0]
                        info[key] = int(val) * 1024
            total = info.get("MemTotal")

            avail = info.get("MemAvailable", info.get("MemFree"))
            if total:
                return total, avail
    except Exception:
        pass

    try:
        if os.name == "nt":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return stat.ullTotalPhys, stat.ullAvailPhys
    except Exception:
        pass

    return None, None


_gpu_cache = {"t": 0.0, "data": None}

def _detect_gpus():
    """Список видеокарт: [{index, name, vram_total_mb, vram_free_mb}]. Кешируется на 5 секунд."""
    now = time.time()
    if _gpu_cache["data"] is not None and now - _gpu_cache["t"] < 5:
        return _gpu_cache["data"]

    gpus = []
    # 1) NVIDIA через nvidia-smi (есть везде, где стоит драйвер; учитывает CUDA_VISIBLE_DEVICES не нужно —
    #    нас интересуют все физические карты)
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.total,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            for line in out.stdout.strip().splitlines():
                parts = [x.strip() for x in line.split(",")]
                if len(parts) >= 4:
                    try:
                        gpus.append({
                            "index": int(parts[0]),
                            "name": parts[1],
                            "vram_total_mb": int(float(parts[2])),
                            "vram_free_mb": int(float(parts[3])),
                        })
                    except ValueError:
                        pass
    except Exception:
        pass

    # 2) запасной вариант — torch (CUDA/ROCm), если nvidia-smi недоступен
    if not gpus:
        try:
            import torch
            if torch.cuda.is_available():
                for i in range(torch.cuda.device_count()):
                    prop = torch.cuda.get_device_properties(i)
                    try:
                        free, total = torch.cuda.mem_get_info(i)
                    except Exception:
                        free, total = 0, prop.total_memory
                    gpus.append({
                        "index": i,
                        "name": prop.name,
                        "vram_total_mb": int(total / (1024 ** 2)),
                        "vram_free_mb": int(free / (1024 ** 2)),
                    })
        except Exception:
            pass

    _gpu_cache["t"] = now
    _gpu_cache["data"] = gpus
    return gpus

def _build_gpu_kwargs(params):
    """Аргументы для Llama(): раскладывает модель по всем видеокартам."""
    kwargs = {"n_gpu_layers": int(params.get("n_gpu_layers", -1))}
    if kwargs["n_gpu_layers"] == 0:
        return kwargs                      # пользователь явно выбрал CPU

    gpus = _detect_gpus()
    if len(gpus) < 2 or not params.get("use_all_gpus", True):
        return kwargs                      # карта одна (или мульти-GPU выключен) — обычный режим

    try:
        import llama_cpp
        split_layer = getattr(llama_cpp, "LLAMA_SPLIT_MODE_LAYER", 1)
    except Exception:
        split_layer = 1

    split = params.get("tensor_split")
    if not (isinstance(split, (list, tuple)) and len(split) == len(gpus) and any(float(x) > 0 for x in split)):
        # авто: пропорционально общему объёму VRAM каждой карты
        split = [float(g["vram_total_mb"]) for g in gpus]
    total = sum(float(x) for x in split) or 1.0
    kwargs["tensor_split"] = [float(x) / total for x in split]
    kwargs["split_mode"] = split_layer
    kwargs["main_gpu"] = 0
    return kwargs

@app.route("/api/local-models/system", methods=["GET"])
def local_models_system():
    total, available = _ram_info()
    try:
        disk_total, _, disk_free = shutil.disk_usage(LOCAL_MODELS_DIR)
    except Exception:
        disk_total, disk_free = None, None

    return jsonify({
        "ram_total_gb": _fmt_gb(total) if total else None,
        "ram_available_gb": _fmt_gb(available) if available else None,
        "ram_total_bytes": total,
        "ram_available_bytes": available,
        "disk_total_gb": _fmt_gb(disk_total) if disk_total else None,
        "disk_free_gb": _fmt_gb(disk_free) if disk_free else None,
        "cpu_count": os.cpu_count() or None,
        "models_dir": LOCAL_MODELS_DIR,
        "gpus": _detect_gpus(),
        "gpu_count": len(_detect_gpus()),
    })

_HF_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")

HF_PAGE_SIZE = 100
_HF_MODELS_URL = f"{HF_API_BASE}/models"

_hf_cursors = {}
_hf_cursors_lock = threading.Lock()

def _hf_fetch(url, params):
    resp = requests.get(url, params=params, headers=_hf_headers(), timeout=20)
    resp.raise_for_status()
    nxt = ((getattr(resp, "links", None) or {}).get("next") or {}).get("url")

    if nxt and not nxt.startswith(_HF_MODELS_URL):
        nxt = None
    return resp.json(), nxt

def _hf_search_page(q, model_type, page, hf_sort="downloads"):
    base = {"filter": "gguf", "sort": hf_sort, "direction": "-1", "limit": HF_PAGE_SIZE}
    if q:
        base["search"] = q
    if model_type:
        base["pipeline_tag"] = model_type

    key = (q, model_type, hf_sort)
    with _hf_cursors_lock:
        if len(_hf_cursors) > 64:
            _hf_cursors.clear()
        cursors = _hf_cursors.setdefault(key, {})
        start = max((p for p in cursors if p <= page), default=0)
        url = cursors.get(start)

    p = start
    while True:
        data, nxt = _hf_fetch(url or _HF_MODELS_URL, None if url else base)
        if nxt:
            with _hf_cursors_lock:
                _hf_cursors.setdefault(key, {})[p + 1] = nxt
        if p == page:
            return data, bool(nxt)
        if not nxt:
            return [], False
        url, p = nxt, p + 1

def _hf_search_multi_type(q, model_types, page, hf_sort="downloads"):
    merged = {}
    any_next = False
    for t in model_types:
        collected = []
        p = 0
        while p <= page:
            data, nxt = _hf_search_page(q, t, p, hf_sort)
            collected = data
            if p == page:
                any_next = any_next or nxt
                break
            if not nxt:
                collected = []
                break
            p += 1
        for m in collected:
            rid = m.get("id") or m.get("modelId")
            if not rid:
                continue

            if rid not in merged:
                merged[rid] = m
    sort_key = (lambda m: m.get("lastModified") or "") if hf_sort == "lastModified" else (lambda m: m.get("downloads", 0))
    ordered = sorted(merged.values(), key=sort_key, reverse=True)
    return ordered, any_next

_PARAM_COUNT_RE = re.compile(r"(?<![a-zA-Z0-9])(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z])")
_PARAM_COUNT_M_RE = re.compile(r"(?<![a-zA-Z0-9])(\d+(?:\.\d+)?)\s*[mM](?![a-zA-Z])")

def _extract_param_count(repo_id):
    if not repo_id:
        return None
    name = repo_id.rsplit("/", 1)[-1]
    m = _PARAM_COUNT_RE.search(name)
    if m:
        return f"{m.group(1)}B"
    m = _PARAM_COUNT_M_RE.search(name)
    if m:
        return f"{m.group(1)}M"
    return None

_UNCENSORED_KEYWORDS = (
    "uncensored", "abliterated", "unaligned", "unfiltered", "unrestricted",
    "nsfw", "no-refusal", "norefusal", "dolphin", "jailbreak", "erotica",
    "unlocked", "de-censored", "decensored",
)

def _is_uncensored_repo(repo_id, tags):
    hay = (repo_id or "").lower() + " " + " ".join(t.lower() for t in (tags or []))
    return any(kw in hay for kw in _UNCENSORED_KEYWORDS)

_repo_sizes_cache = {}
_repo_sizes_lock = threading.Lock()
_MB = 1024 * 1024

def _repo_gguf_sizes(repo_id):
    """Размеры всех .gguf файлов репозитория (в байтах). Кешируется, чтобы не дёргать HF повторно."""
    with _repo_sizes_lock:
        if repo_id in _repo_sizes_cache:
            return _repo_sizes_cache[repo_id]
    try:
        resp = requests.get(f"{HF_API_BASE}/models/{repo_id}", params={"blobs": "true"},
                            headers=_hf_headers(), timeout=15)
        resp.raise_for_status()
        sizes = []
        for sib in resp.json().get("siblings", []):
            if (sib.get("rfilename") or "").lower().endswith(".gguf"):
                size = sib.get("size") or (sib.get("lfs") or {}).get("size")
                if size:
                    sizes.append(int(size))
    except Exception:
        return None          # не смогли узнать — не выбрасываем модель из выдачи
    with _repo_sizes_lock:
        if len(_repo_sizes_cache) > 2000:
            _repo_sizes_cache.clear()
        _repo_sizes_cache[repo_id] = sizes
    return sizes

def _filter_by_size(items, min_mb, max_mb):
    """Оставляет репозитории, где есть хотя бы один .gguf файл размером в [min_mb, max_mb] МБ."""
    from concurrent.futures import ThreadPoolExecutor
    lo = (min_mb * _MB) if min_mb is not None else None
    hi = (max_mb * _MB) if max_mb is not None else None

    def check(m):
        rid = m.get("id") or m.get("modelId")
        if not rid:
            return False
        sizes = _repo_gguf_sizes(rid)
        if sizes is None:
            return True
        return any((lo is None or sz >= lo) and (hi is None or sz <= hi) for sz in sizes)

    with ThreadPoolExecutor(max_workers=16) as pool:
        keep = list(pool.map(check, items))
    return [m for m, ok in zip(items, keep) if ok]

def _parse_mb(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        v = float(value)
    except ValueError:
        return None
    return max(0.0, v)

@app.route("/api/local-models/search", methods=["GET"])
def local_models_search():
    # Без ключа Hugging Face поиск не запускаем вовсе: анонимные запросы быстро
    # упираются в 429. Фронтенд по коду hf_key_required показывает форму ввода ключа.
    if not _hf_has_key():
        return jsonify({
            "error": "Для поиска моделей нужен ключ Hugging Face (бесплатный).",
            "code": "hf_key_required",
        }), 401
    q = (request.args.get("q") or "").strip()
    type_param = (request.args.get("type") or "").strip().lower()
    model_types = [t.strip() for t in type_param.split(",") if t.strip()]
    censorship = (request.args.get("censorship") or "all").strip().lower()
    if censorship not in ("all", "censored", "uncensored"):
        censorship = "all"
    sort_by = (request.args.get("sort") or "downloads").strip().lower()
    if sort_by not in ("downloads", "date"):
        sort_by = "downloads"
    try:
        page = max(0, min(int(request.args.get("page") or 0), 500))
    except ValueError:
        page = 0
    try:
        hf_start = max(0, min(int(request.args.get("hf_start") or 0), 5000))
    except ValueError:
        hf_start = 0
    min_mb = _parse_mb(request.args.get("min_mb"))
    max_mb = _parse_mb(request.args.get("max_mb"))
    size_filter = min_mb is not None or max_mb is not None
    if size_filter and min_mb is not None and max_mb is not None and min_mb > max_mb:
        min_mb, max_mb = max_mb, min_mb
    for t in model_types:
        if not _HF_TYPE_RE.match(t):
            return jsonify({"error": "Некорректный тип модели"}), 400

    try:
        hf_sort = "lastModified" if sort_by == "date" else "downloads"

        def fetch_page(pg):
            if len(model_types) > 1:
                return _hf_search_multi_type(q, model_types, pg, hf_sort)
            return _hf_search_page(q, model_types[0] if model_types else "", pg, hf_sort)

        def keep_by_censorship(m):
            if censorship == "all":
                return True
            is_unc = _is_uncensored_repo(m.get("id") or m.get("modelId"), m.get("tags"))
            return (censorship == "uncensored") == is_unc

        hf_page = page
        if censorship == "all" and not size_filter:
            data, has_next = fetch_page(page)
        else:
            # Фильтры (цензура / размер) отсеивают часть выдачи — добираем следующие
            # страницы HF, пока не наберём 100 подходящих или не кончатся попытки.
            data, has_next = [], False
            hf_page = hf_start
            attempts = 0
            max_attempts = 8 if size_filter else 5
            while len(data) < HF_PAGE_SIZE and attempts < max_attempts:
                page_data, nxt = fetch_page(hf_page)
                page_data = [m for m in page_data if keep_by_censorship(m)]
                if size_filter:
                    page_data = _filter_by_size(page_data, min_mb, max_mb)
                data.extend(page_data)
                has_next = nxt
                attempts += 1
                hf_page += 1          # курсор всегда указывает на СЛЕДУЮЩУЮ, ещё не прочитанную страницу HF
                if not nxt:
                    break
            # Намеренно НЕ обрезаем до 100: иначе хвост последней прочитанной страницы HF
            # потерялся бы (курсор уже ушёл дальше). Лишние модели — не проблема.
    except Exception as e:
        return jsonify({"error": f"Не удалось получить список моделей: {e}"}), 502

    next_hf_start = hf_page if (censorship != "all" or size_filter) else page + 1
    results = []
    for m in data:
        repo_id = m.get("id") or m.get("modelId")
        results.append({
            "repo_id": repo_id,
            "downloads": m.get("downloads", 0),
            "likes": m.get("likes", 0),
            "pipeline_tag": m.get("pipeline_tag"),
            "tags": m.get("tags", []),
            "updated_at": m.get("lastModified"),
            "uncensored": _is_uncensored_repo(repo_id, m.get("tags")),
            "param_count": _extract_param_count(repo_id),
        })
    return jsonify({"results": results, "page": page, "page_size": HF_PAGE_SIZE, "has_next": has_next,
                    "next_hf_start": next_hf_start})

@app.route("/api/local-models/repo-files", methods=["GET"])
def local_models_repo_files():
    repo_id = (request.args.get("repo_id") or "").strip()
    if not repo_id:
        return jsonify({"error": "Не указан repo_id"}), 400

    try:
        resp = requests.get(f"{HF_API_BASE}/models/{repo_id}", params={"blobs": "true"},
                             headers=_hf_headers(), timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return jsonify({"error": f"Не удалось получить файлы репозитория: {e}"}), 502

    files = []
    for sib in data.get("siblings", []):
        name = sib.get("rfilename") or ""
        if name.lower().endswith(".gguf"):
            size = sib.get("size") or (sib.get("lfs") or {}).get("size")
            files.append({
                "filename": name,
                "size_bytes": size,
                "size_gb": _fmt_gb(size) if size else None,

                "param_count": _extract_param_count(name) or _extract_param_count(repo_id),
            })

    files = _group_shards(files)
    files.sort(key=lambda f: f["filename"])
    return jsonify({"repo_id": repo_id, "files": files, "param_count": _extract_param_count(repo_id)})

_download_tasks = {}
_download_tasks_lock = threading.Lock()

def _download_one(task_id, repo_id, filename, dest, cancel_event, done_before):
    """Качает один файл в dest (через .part). Общий прогресс = done_before + скачано."""
    tmp_path = dest + ".part"
    url = f"https://huggingface.co/{repo_id}/resolve/main/{filename}"
    with requests.get(url, headers=_hf_headers(), stream=True, timeout=30,
                       params={"download": "true"}) as resp:
        if resp.status_code in (401, 403):
            raise RuntimeError("Модель требует доступа/токена Hugging Face (гейтед репозиторий)")
        resp.raise_for_status()
        downloaded = 0
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK):
                if cancel_event.is_set():
                    raise InterruptedError("cancelled")
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                with _download_tasks_lock:
                    _download_tasks[task_id]["downloaded"] = done_before + downloaded
    os.replace(tmp_path, dest)
    return os.path.getsize(dest)

def _run_download(task_id, repo_id, filename, dest_path, cancel_event, part_files=None):
    """part_files=None — обычный один файл. Иначе — список ВСЕХ частей многофайловой модели:
    они кладутся в одну папку под оригинальными именами (иначе llama.cpp не найдёт остальные части)."""
    created = []
    try:
        if not part_files:
            size_bytes = _download_one(task_id, repo_id, filename, dest_path, cancel_event, 0)
            created.append(dest_path)
            model_path, folder = dest_path, None
        else:
            folder = dest_path                     # для многочастной модели dest_path — это папка
            os.makedirs(folder, exist_ok=True)
            size_bytes, model_path = 0, None
            for pf in part_files:
                target = os.path.join(folder, os.path.basename(pf))
                got = _download_one(task_id, repo_id, pf, target, cancel_event, size_bytes)
                created.append(target)
                size_bytes += got
                if pf == filename:
                    model_path = target            # запускаем по первой части
        model_id = _safe_id(repo_id, filename)
        with _storage_lock:
            _models_store["models"][model_id] = {
                "id": model_id,
                "repo_id": repo_id,
                "filename": filename,
                "path": model_path,
                "folder": folder,
                "parts": len(part_files) if part_files else 1,
                "size_bytes": size_bytes,
                "downloaded_at": time.time(),
                "params": dict(_DEFAULT_PARAMS),
            }
        _persist_models()
        with _download_tasks_lock:
            _download_tasks[task_id]["status"] = "done"
            _download_tasks[task_id]["model_id"] = model_id

    except InterruptedError:
        _cleanup_partial(created, dest_path, part_files)
        with _download_tasks_lock:
            _download_tasks[task_id]["status"] = "cancelled"
    except Exception as e:
        _cleanup_partial(created, dest_path, part_files)
        with _download_tasks_lock:
            _download_tasks[task_id]["status"] = "error"
            _download_tasks[task_id]["error"] = str(e)

def _cleanup_partial(created, dest_path, part_files):
    """Ошибка/отмена на полпути: не оставляем на диске полмодели (она всё равно не запустится)."""
    if part_files:
        shutil.rmtree(dest_path, ignore_errors=True)
    else:
        for path in created + [dest_path + ".part"]:
            _safe_remove(path)

def _safe_remove(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass

@app.route("/api/local-models/download", methods=["POST"])
def local_models_download():
    body = request.get_json(force=True) or {}
    repo_id = (body.get("repo_id") or "").strip()
    filename = (body.get("filename") or "").strip()
    if not repo_id or not filename:
        return jsonify({"error": "Нужно указать repo_id и filename"}), 400
    if not filename.lower().endswith(".gguf"):
        return jsonify({"error": "Поддерживаются только .gguf файлы"}), 400

    model_id = _safe_id(repo_id, filename)
    dest_path = os.path.join(LOCAL_MODELS_DIR, model_id)

    # Многофайловая модель (X-00001-of-0000N.gguf): качаем ВСЕ части в одну папку.
    part_files = None
    info = _shard_info(filename)
    if info:
        base, idx, total = info
        if idx != 1:
            return jsonify({"error": "Выбери первую часть (…-00001-of-…): остальные скачаются сами"}), 400
        folder_in_repo, _, name = filename.rpartition("/")
        stem = _SHARD_RE.match(name).group("base")
        prefix = (folder_in_repo + "/" if folder_in_repo else "") + stem
        part_files = [f"{prefix}-{i:05d}-of-{total:05d}.gguf" for i in range(1, total + 1)]
        dest_path = os.path.join(LOCAL_MODELS_DIR, model_id[:-5] if model_id.lower().endswith(".gguf") else model_id)

    with _download_tasks_lock:
        for t in _download_tasks.values():
            if t.get("model_id_pending") == model_id and t.get("status") == "downloading":
                return jsonify({"error": "Эта модель уже скачивается", "task_id": t["task_id"]}), 409

    task_id = uuid.uuid4().hex
    cancel_event = threading.Event()
    with _download_tasks_lock:
        _download_tasks[task_id] = {
            "task_id": task_id,
            "repo_id": repo_id,
            "filename": filename,
            "model_id_pending": model_id,
            "status": "downloading",
            "downloaded": 0,
            "total": body.get("size_bytes") or 0,
            "started_at": time.time(),
            "_cancel_event": cancel_event,
        }

    th = threading.Thread(target=_run_download, args=(task_id, repo_id, filename, dest_path, cancel_event, part_files), daemon=True)
    th.start()
    return jsonify({"ok": True, "task_id": task_id})

def _public_task(t):
    return {k: v for k, v in t.items() if not k.startswith("_")}

@app.route("/api/local-models/downloads", methods=["GET"])
def local_models_downloads():
    with _download_tasks_lock:
        tasks = [_public_task(t) for t in _download_tasks.values()]
    return jsonify({"downloads": tasks})

@app.route("/api/local-models/downloads/<task_id>/cancel", methods=["POST"])
def local_models_cancel_download(task_id):
    with _download_tasks_lock:
        task = _download_tasks.get(task_id)
        if not task:
            return jsonify({"error": "Задача не найдена"}), 404
        cancel_event = task.get("_cancel_event")
    if cancel_event:
        cancel_event.set()
    return jsonify({"ok": True})

_ALLOWED_UPLOAD_EXT = (".gguf",)

@app.route("/api/local-models/upload", methods=["POST"])
def local_models_upload():
    """Загрузка .gguf файла с диска пользователя (без Hugging Face).
    Файл кладётся как есть в LOCAL_MODELS_DIR и регистрируется в списке моделей,
    как будто он был скачан обычным способом."""
    f = request.files.get("file")
    if f is None or not (f.filename or "").strip():
        return jsonify({"error": "Файл не передан"}), 400

    orig_name = f.filename.strip().replace("\\", "/").rsplit("/", 1)[-1]
    if not orig_name.lower().endswith(_ALLOWED_UPLOAD_EXT):
        return jsonify({"error": "Поддерживаются только .gguf файлы"}), 400

    repo_id = "local-upload"
    model_id = _safe_id(repo_id, orig_name)
    dest_path = os.path.join(LOCAL_MODELS_DIR, model_id)
    tmp_path = dest_path + ".part"

    try:
        os.makedirs(LOCAL_MODELS_DIR, exist_ok=True)
        with open(tmp_path, "wb") as out:
            while True:
                chunk = f.stream.read(DOWNLOAD_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
        os.replace(tmp_path, dest_path)
    except Exception as e:
        _safe_remove(tmp_path)
        return jsonify({"error": f"Не удалось сохранить файл: {e}"}), 500

    size_bytes = os.path.getsize(dest_path)
    with _storage_lock:
        _models_store["models"][model_id] = {
            "id": model_id,
            "repo_id": repo_id,
            "filename": orig_name,
            "path": dest_path,
            "folder": None,
            "parts": 1,
            "size_bytes": size_bytes,
            "downloaded_at": time.time(),
            "params": dict(_DEFAULT_PARAMS),
        }
    _persist_models()
    return jsonify({"ok": True, "model_id": model_id})


def _scan_dirs():
    """Папки, которые сканируем на предмет уже лежащих .gguf файлов:
    основная папка моделей плюс, если задан путь к llama-server, папка рядом с ним."""
    dirs = [LOCAL_MODELS_DIR]
    server_bin = (os.environ.get("LLAMA_SERVER_BIN") or "").strip()
    if server_bin and os.path.isfile(server_bin):
        dirs.append(os.path.dirname(os.path.abspath(server_bin)))
    extra = (os.environ.get("LLAMA_MODELS_SCAN_DIR") or "").strip()
    if extra:
        dirs.append(extra)
    seen, out = set(), []
    for d in dirs:
        ad = os.path.abspath(d)
        if ad not in seen and os.path.isdir(ad):
            seen.add(ad)
            out.append(ad)
    return out


@app.route("/api/local-models/scan", methods=["POST"])
def local_models_scan():
    """Ищет .gguf файлы на диске (в LOCAL_MODELS_DIR и рядом с llama-server),
    которых ещё нет в списке моделей, и добавляет их — без скачивания."""
    with _storage_lock:
        known_paths = {os.path.abspath(m.get("path", "")) for m in _models_store["models"].values() if m.get("path")}

    added = []
    for d in _scan_dirs():
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in sorted(names):
            if not name.lower().endswith(".gguf"):
                continue
            shard = _shard_info(name)
            if shard and shard[1] != 1:
                continue  # многочастная модель — регистрируем только по первой части
            full = os.path.join(d, name)
            if not os.path.isfile(full):
                continue
            if os.path.abspath(full) in known_paths:
                continue
            if _missing_shards(full):
                continue  # неполный набор частей — пропускаем

            repo_id = "local-scan"
            model_id = _safe_id(repo_id, name)
            with _storage_lock:
                if model_id in _models_store["models"]:
                    continue
                _models_store["models"][model_id] = {
                    "id": model_id,
                    "repo_id": repo_id,
                    "filename": name,
                    "path": full,
                    "folder": None,
                    "parts": 1,
                    "size_bytes": os.path.getsize(full),
                    "downloaded_at": time.time(),
                    "params": dict(_DEFAULT_PARAMS),
                }
            known_paths.add(os.path.abspath(full))
            added.append(model_id)
    if added:
        _persist_models()
    return jsonify({"ok": True, "added": added, "scanned_dirs": _scan_dirs()})


@app.route("/api/local-models/list", methods=["GET"])
def local_models_list():
    models = []
    for model_id, m in _models_store["models"].items():
        entry = dict(m)
        entry["exists"] = os.path.exists(m.get("path", "")) and not _missing_shards(m.get("path", ""))
        entry["size_gb"] = _fmt_gb(m.get("size_bytes"))
        merged = dict(_DEFAULT_PARAMS)
        merged.update(entry.get("params") or {})
        entry["params"] = merged
        models.append(entry)
    models.sort(key=lambda m: m.get("downloaded_at", 0), reverse=True)
    return jsonify({"models": models})

@app.route("/api/local-models/<model_id>", methods=["DELETE"])
def local_models_delete(model_id):
    with _storage_lock:
        m = _models_store["models"].pop(model_id, None)
    if not m:
        return jsonify({"error": "Модель не найдена"}), 404
    _unload_if_loaded(model_id)
    if m.get("folder"):
        shutil.rmtree(m["folder"], ignore_errors=True)
    else:
        _safe_remove(m.get("path", ""))
    _persist_models()
    return jsonify({"ok": True})

@app.route("/api/local-models/<model_id>/params", methods=["POST"])
def local_models_set_params(model_id):
    body = request.get_json(force=True) or {}
    with _storage_lock:
        m = _models_store["models"].get(model_id)
        if not m:
            return jsonify({"error": "Модель не найдена"}), 404
        params = m.setdefault("params", dict(_DEFAULT_PARAMS))
        for key in ("n_ctx", "n_gpu_layers", "n_threads"):
            if key in body:
                try:
                    params[key] = int(body[key])
                except (TypeError, ValueError):
                    pass
        if "use_all_gpus" in body:
            v = body["use_all_gpus"]
            params["use_all_gpus"] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
        if "tensor_split" in body:
            ts = body["tensor_split"]
            if ts in (None, "", []):
                params["tensor_split"] = None
            else:
                try:
                    if isinstance(ts, str):
                        ts = [x for x in re.split(r"[\s,;:/]+", ts.strip()) if x]
                    ts = [max(0.0, float(x)) for x in ts]
                    params["tensor_split"] = ts if any(x > 0 for x in ts) else None
                except (TypeError, ValueError):
                    pass
        if "temperature" in body:
            try:
                params["temperature"] = float(body["temperature"])
            except (TypeError, ValueError):
                pass
    _persist_models()

    _unload_if_loaded(model_id)
    return jsonify({"ok": True, "params": m["params"]})

_llama_lock = threading.Lock()
_loaded = {"model_id": None, "llm": None}

import atexit
atexit.register(lambda: _release_llm(_loaded.get("llm")))

def _find_model_meta(model_ref):
    with _storage_lock:
        m = _models_store["models"].get(model_ref)
        if m:
            return dict(m)
        for m in _models_store["models"].values():
            if m.get("filename") == model_ref:
                return dict(m)
    return None

def _unload_if_loaded(model_id):
    with _llama_lock:
        if _loaded["model_id"] == model_id and _loaded["llm"] is not None:
            _release_llm(_loaded["llm"])
            _loaded["llm"] = None
            _loaded["model_id"] = None
            gc.collect()

def _unload_current():
    with _llama_lock:
        if _loaded["llm"] is not None:
            _release_llm(_loaded["llm"])
            _loaded["llm"] = None
            _loaded["model_id"] = None
            gc.collect()

class _ServerLLM:
    """Обёртка над запущенным llama-server: даёт тот же create_chat_completion(), что и Llama()."""

    def __init__(self, proc, port, log_path):
        self.proc = proc
        self.port = port
        self.log_path = log_path
        self.base = f"http://127.0.0.1:{port}"

    def alive(self):
        return self.proc.poll() is None

    def create_chat_completion(self, messages, temperature=0.7, max_tokens=None):
        body = {"messages": messages, "temperature": temperature, "stream": False}
        if max_tokens:
            body["max_tokens"] = int(max_tokens)
        r = requests.post(self.base + "/v1/chat/completions", json=body, timeout=1800)
        if r.status_code != 200:
            raise RuntimeError(f"llama-server ответил {r.status_code}: {r.text[:300]}")
        return r.json()

    def close(self):
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=8)
                except Exception:
                    self.proc.kill()
        except Exception:
            pass

    def __del__(self):
        self.close()


def _missing_shards(path):
    """Для файла вида X-00001-of-0000N.gguf возвращает имена частей, которых нет рядом на диске."""
    m = _SHARD_RE.match(os.path.basename(path))
    if not m:
        return []
    folder = os.path.dirname(path)
    total = int(m.group("total"))
    names = [f"{m.group('base')}-{i:05d}-of-{total:05d}.gguf" for i in range(1, total + 1)]
    return [n for n in names if not os.path.exists(os.path.join(folder, n))]


def _find_llama_server():
    """Ищет бинарник llama-server: переменная LLAMA_SERVER_BIN, PATH, типичные места Termux."""
    env = (os.environ.get("LLAMA_SERVER_BIN") or "").strip()
    if env and os.path.isfile(env):
        return env
    found = shutil.which("llama-server")
    if found:
        return found
    for cand in ("/data/data/com.termux/files/usr/bin/llama-server",
                 os.path.expanduser("~/llama.cpp/build/bin/llama-server")):
        if os.path.isfile(cand):
            return cand
    return None


def _free_port():
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_llama_server(path, params):
    binary = _find_llama_server()
    if not binary:
        raise RuntimeError(
            "Не найден ни llama-cpp-python, ни llama-server. В Termux выполни: "
            "pkg install llama-cpp  (или задай путь в LLAMA_SERVER_BIN)."
        )
    port = _free_port()
    cmd = [binary, "-m", path, "--host", "127.0.0.1", "--port", str(port),
           "-c", str(int(params.get("n_ctx", 4096))),
           "-t", str(int(params.get("n_threads", os.cpu_count() or 4)))]
    ngl = int(params.get("n_gpu_layers", 0) or 0)
    if ngl != 0:
        cmd += ["-ngl", "999" if ngl < 0 else str(ngl)]
    log_path = os.path.join(os.path.dirname(path) or ".", "llama-server.log")
    try:
        logf = open(log_path, "wb")
    except OSError:
        logf = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    except OSError as e:
        raise RuntimeError(f"Не удалось запустить llama-server: {e}")
    llm = _ServerLLM(proc, port, log_path)

    # Ждём, пока модель загрузится (на телефоне это может занять минуту-две).
    deadline = time.time() + 300
    while time.time() < deadline:
        if not llm.alive():
            tail = ""
            try:
                with open(log_path, "rb") as f:
                    tail = f.read()[-600:].decode("utf-8", "replace")
            except OSError:
                pass
            raise RuntimeError("llama-server завершился при запуске (не хватило памяти или файл повреждён). "
                               "Попробуй модель поменьше / контекст меньше. Лог: " + tail)
        try:
            h = requests.get(llm.base + "/health", timeout=2)
            if h.status_code == 200:
                return llm
        except requests.RequestException:
            pass
        time.sleep(1)
    llm.close()
    raise RuntimeError("llama-server не успел загрузить модель за 5 минут")


def _release_llm(llm):
    if isinstance(llm, _ServerLLM):
        llm.close()


def _get_llama_for(meta):
    model_id = meta["id"]
    path = meta.get("path", "")
    if not os.path.exists(path):
        raise RuntimeError("Файл модели не найден на диске — скачай её заново")
    missing = _missing_shards(path)
    if missing:
        raise RuntimeError(
            f"Модель разрезана на части, а на диске не хватает {len(missing)} шт. (например {missing[0]}). "
            "Удали её в панели и скачай заново — теперь скачиваются все части сразу."
        )

    try:
        from llama_cpp import Llama
    except ImportError:
        Llama = None

    with _llama_lock:
        cur = _loaded["llm"]
        if _loaded["model_id"] == model_id and cur is not None:
            if not isinstance(cur, _ServerLLM) or cur.alive():
                return cur

        if cur is not None:
            _release_llm(cur)
            _loaded["llm"] = None
            _loaded["model_id"] = None
            gc.collect()

        params = dict(_DEFAULT_PARAMS)
        params.update(meta.get("params") or {})

        if Llama is None:
            # llama-cpp-python нет (например, Termux) — запускаем llama-server.
            llm = _start_llama_server(path, params)
        else:
            gpu_kwargs = _build_gpu_kwargs(params)
            try:
                llm = Llama(
                    model_path=path,
                    n_ctx=int(params.get("n_ctx", 4096)),
                    n_threads=int(params.get("n_threads", os.cpu_count() or 4)),
                    chat_format=None,
                    verbose=False,
                    **gpu_kwargs,
                )
            except Exception as e:
                # Если мульти-GPU раскладка не подошла — пробуем без неё, а не падаем сразу.
                if "tensor_split" in gpu_kwargs:
                    fallback = {"n_gpu_layers": gpu_kwargs["n_gpu_layers"]}
                    try:
                        llm = Llama(
                            model_path=path,
                            n_ctx=int(params.get("n_ctx", 4096)),
                            n_threads=int(params.get("n_threads", os.cpu_count() or 4)),
                            chat_format=None,
                            verbose=False,
                            **fallback,
                        )
                    except Exception as e2:
                        raise RuntimeError(f"Не удалось загрузить модель: {e2}")
                else:
                    raise RuntimeError(f"Не удалось загрузить модель: {e}")

        _loaded["llm"] = llm
        _loaded["model_id"] = model_id
        return llm

def list_installed_model_names():
    with _storage_lock:
        return [m["filename"] for m in _models_store["models"].values() if os.path.exists(m.get("path", "")) and not _missing_shards(m.get("path", ""))]

def call_local(model, messages, system_prompt, temperature, max_tokens, key, attachments, code_execution=False, web_search=False):
    meta = _find_model_meta(model)
    if not meta:
        raise ProviderError(404, f"Локальная модель '{model}' не найдена — скачай её в панели «Локальные модели».")

    try:
        llm = _get_llama_for(meta)
    except RuntimeError as e:
        raise ProviderError(400, str(e))

    msgs = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})
    msgs.extend({"role": m["role"], "content": m.get("content", "")} for m in messages)

    params = meta.get("params") or {}
    try:
        with _llama_lock:
            out = llm.create_chat_completion(
                messages=msgs,
                temperature=params.get("temperature", temperature),
                max_tokens=max_tokens,
            )
    except Exception as e:
        raise ProviderError(500, f"Ошибка локального инференса: {e}")

    try:
        text = out["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError):
        raise ProviderError(500, f"Не удалось разобрать ответ локальной модели: {str(out)[:300]}")

    usage = out.get("usage")
    if usage:
        record_usage(usage, "openai")

    if attachments:
        text += "\n\n[Внимание: локальные модели пока не умеют читать вложения — файлы не были отправлены]"

    return text, []

@app.route("/api/local-models/runtime", methods=["GET"])
def local_models_runtime():
    with _llama_lock:
        loaded_id = _loaded["model_id"]
    filename = None
    if loaded_id:
        with _storage_lock:
            m = _models_store["models"].get(loaded_id)
            filename = m.get("filename") if m else loaded_id
    return jsonify({"loaded_model_id": loaded_id, "loaded_model_filename": filename})

@app.route("/api/local-models/<model_id>/unload", methods=["POST"])
def local_models_unload(model_id):
    _unload_if_loaded(model_id)
    return jsonify({"ok": True})

from p10_providers import PROVIDERS, ProviderError
from p16_usage import record_usage

PROVIDERS["local"] = call_local
