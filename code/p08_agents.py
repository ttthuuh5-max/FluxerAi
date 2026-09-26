from flask import jsonify
from flask import request
from p01_app import app
from p03_storage import _load_json, _save_json, _storage_lock

from config.settings import AGENTS_FILE, MAX_AGENTS

_DEFAULT_AGENT = {
    "enabled": False,
    "name": "",
    "prompt": "",
    "provider": "",
    "model": "",
    "api_key": "",
}

def _default_agents_store():
    return {"agents": [dict(_DEFAULT_AGENT, slot=i, id=f"agent_{i}") for i in range(1, MAX_AGENTS + 1)]}

_agents_store = _load_json(AGENTS_FILE, _default_agents_store())

if not isinstance(_agents_store, dict) or not isinstance(_agents_store.get("agents"), list):
    _agents_store = _default_agents_store()
else:
    _by_slot = {a.get("slot"): a for a in _agents_store["agents"] if isinstance(a, dict)}
    _fixed = []
    for _i in range(1, MAX_AGENTS + 1):
        _a = dict(_DEFAULT_AGENT)
        _a.update(_by_slot.get(_i, {}))
        _a["slot"] = _i
        _a["id"] = _a.get("id") or f"agent_{_i}"
        _fixed.append(_a)
    _agents_store["agents"] = _fixed

def _persist_agents():
    _save_json(AGENTS_FILE, _agents_store)

def _agent_by_slot(slot):
    for a in _agents_store["agents"]:
        if a.get("slot") == slot:
            return a
    return None

@app.route("/api/agents", methods=["GET"])
def list_agents():
    return jsonify({"agents": _agents_store["agents"]})

@app.route("/api/agents/<int:slot>", methods=["POST"])
def update_agent(slot):
    if slot < 1 or slot > MAX_AGENTS:
        return jsonify({"error": f"slot должен быть от 1 до {MAX_AGENTS}"}), 400
    body = request.get_json(force=True) or {}
    with _storage_lock:
        agent = _agent_by_slot(slot)
        if agent is None:
            agent = dict(_DEFAULT_AGENT, slot=slot, id=f"agent_{slot}")
            _agents_store["agents"].append(agent)
        for key in ("enabled", "name", "prompt", "provider", "model", "api_key"):
            if key in body:
                agent[key] = body[key]
        saved = dict(agent)
    _persist_agents()
    from p12_tools import _refresh_agent_tools
    _refresh_agent_tools()
    return jsonify({"ok": True, "agent": saved})

@app.route("/api/agents/<int:slot>", methods=["DELETE"])
def reset_agent(slot):
    if slot < 1 or slot > MAX_AGENTS:
        return jsonify({"error": f"slot должен быть от 1 до {MAX_AGENTS}"}), 400
    with _storage_lock:
        agent = _agent_by_slot(slot)
        if agent is not None:
            agent.update(dict(_DEFAULT_AGENT))
            agent["slot"] = slot
            agent["id"] = f"agent_{slot}"
    _persist_agents()
    from p12_tools import _refresh_agent_tools
    _refresh_agent_tools()
    return jsonify({"ok": True})
