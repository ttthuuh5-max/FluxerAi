from datetime import datetime
from datetime import timezone
from flask import jsonify
from flask import request
import time
from p01_app import app
from p03_storage import PROMPTS_FILE, STORE_PROMPTS_FILE, _load_json, _save_json, _storage_lock

_prompts_store = _load_json(PROMPTS_FILE, {"prompts": []})
if not isinstance(_prompts_store.get("prompts"), list):
    _prompts_store["prompts"] = []

def _persist_prompts():
    _save_json(PROMPTS_FILE, _prompts_store)

@app.route("/api/prompts", methods=["GET"])
def list_prompts():
    return jsonify({"prompts": _prompts_store["prompts"]})

@app.route("/api/prompts", methods=["POST"])
def create_or_update_prompt():
    body = request.get_json(force=True) or {}
    name = (body.get("name") or "").strip()
    content = body.get("content") or ""
    if not name:
        return jsonify({"error": "Не указано имя промта"}), 400

    prompt_id = body.get("id")
    now = datetime.now(timezone.utc).isoformat()

    with _storage_lock:
        prompts = _prompts_store["prompts"]
        if prompt_id:
            idx = next((i for i, p in enumerate(prompts) if p.get("id") == prompt_id), None)
        else:
            idx = None
        if idx is not None:
            prompts[idx]["name"] = name
            prompts[idx]["content"] = content
            prompts[idx]["updated_at"] = now
            saved = prompts[idx]
        else:
            saved = {
                "id": prompt_id or ("prompt_" + str(int(time.time() * 1000))),
                "name": name,
                "content": content,
                "updated_at": now,
            }
            prompts.insert(0, saved)
    _persist_prompts()
    return jsonify({"ok": True, "prompt": saved})

@app.route("/api/prompts/<prompt_id>", methods=["DELETE"])
def delete_prompt(prompt_id):
    with _storage_lock:
        before = len(_prompts_store["prompts"])
        _prompts_store["prompts"] = [p for p in _prompts_store["prompts"] if p.get("id") != prompt_id]
        removed = before - len(_prompts_store["prompts"])
    _persist_prompts()
    return jsonify({"ok": True, "removed": removed})

_store_prompts_data = _load_json(STORE_PROMPTS_FILE, {"prompts": []})
if not isinstance(_store_prompts_data.get("prompts"), list):
    _store_prompts_data["prompts"] = []

def _build_store_index():
    items = _store_prompts_data["prompts"]
    blobs = []
    counts = {}
    for p in items:
        kw = p.get("keywords")
        kw_text = "\n".join(kw) if isinstance(kw, list) else ""
        blobs.append(((p.get("name") or "") + "\n" + (p.get("content") or "") + "\n" + kw_text).lower())
        cat = p.get("category") or "Разное"
        counts[cat] = counts.get(cat, 0) + 1
    categories = [{"name": n, "count": c} for n, c in sorted(counts.items(), key=lambda kv: -kv[1])]
    return blobs, categories

_store_blobs, _store_categories = _build_store_index()

# Сколько символов текста отдавать в списке. Карточка показывает только 3 строки превью,
# а сами промты бывают до 475 КБ — раньше список из 40 карточек весил ~3 МБ.
_PREVIEW_CHARS = 320
_store_by_id = {}

def _rebuild_id_index():
    global _store_by_id
    _store_by_id = {p.get("id"): p for p in _store_prompts_data["prompts"] if p.get("id")}

_rebuild_id_index()

def _preview_of(p):
    text = p.get("content") or ""
    item = {k: v for k, v in p.items() if k not in ("content", "keywords")}
    item["content"] = text[:_PREVIEW_CHARS]
    item["truncated"] = len(text) > _PREVIEW_CHARS
    item["length"] = len(text)
    return item

@app.route("/api/store_prompts", methods=["GET"])
def list_store_prompts():
    q = (request.args.get("q") or "").strip().lower()
    category = (request.args.get("category") or "").strip()
    limit_raw = request.args.get("limit")
    try:
        limit = int(limit_raw) if limit_raw else 60
    except ValueError:
        limit = 60
    limit = max(1, min(limit, 300))
    try:
        offset = int(request.args.get("offset") or 0)
    except ValueError:
        offset = 0
    offset = max(0, offset)

    items = _store_prompts_data["prompts"]

    idx = range(len(items))
    if category:
        idx = [i for i in idx if (items[i].get("category") or "Разное") == category]
    if q:
        idx = [i for i in idx if q in _store_blobs[i]]

    total = len(idx)
    page = [_preview_of(items[i]) for i in idx[offset:offset + limit]]
    return jsonify({"prompts": page, "total": total, "offset": offset, "limit": limit})

@app.route("/api/store_prompts/<prompt_id>", methods=["GET"])
def get_store_prompt(prompt_id):
    """Полный текст одного промта — подгружается по клику «Использовать» / «Сохранить как роль»."""
    p = _store_by_id.get(prompt_id)
    if p is None:
        return jsonify({"error": "Промт не найден"}), 404
    return jsonify({"prompt": p})

@app.route("/api/store_prompts/categories", methods=["GET"])
def list_store_prompt_categories():
    return jsonify({"categories": _store_categories, "total": len(_store_prompts_data["prompts"])})
