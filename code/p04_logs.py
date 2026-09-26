from datetime import datetime
from datetime import timezone
from flask import jsonify
from flask import request
import json
import os
import sqlite3
import threading
import time
import traceback
from p01_app import app
from config.settings import LOGS_DB_FILE

_logs_db_lock = threading.Lock()

def _logs_db_connect():
    conn = sqlite3.connect(LOGS_DB_FILE, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def _init_logs_db():
    with _logs_db_lock:
        conn = _logs_db_connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    provider TEXT,
                    requested_provider TEXT,
                    model TEXT,
                    auto_detected INTEGER DEFAULT 0,
                    status TEXT NOT NULL,
                    duration_ms INTEGER,
                    system_prompt TEXT,
                    request_messages TEXT,
                    attachments_meta TEXT,
                    final_reply TEXT,
                    error TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS request_rounds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id INTEGER NOT NULL,
                    round_index INTEGER NOT NULL,
                    raw_model_reply TEXT,
                    clean_reply TEXT,
                    tool_calls TEXT,
                    tool_results TEXT,
                    entry_kind TEXT DEFAULT 'round',
                    FOREIGN KEY(request_id) REFERENCES request_logs(id)
                )
            """)

            try:
                conn.execute("ALTER TABLE request_rounds ADD COLUMN entry_kind TEXT DEFAULT 'round'")
            except Exception:
                pass

            for col in ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"):
                try:
                    conn.execute(f"ALTER TABLE request_logs ADD COLUMN {col} INTEGER")
                except Exception:
                    pass
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rounds_request_id ON request_rounds(request_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_created_at ON request_logs(created_at)")
            conn.commit()
        finally:
            conn.close()

_init_logs_db()

class RequestLogger:

    def __init__(self, requested_provider, model, system_prompt, messages, attachments):
        self.requested_provider = requested_provider or ""
        self.model = model or ""
        self.system_prompt = system_prompt or ""
        try:
            self.request_messages = json.dumps(messages, ensure_ascii=False)
        except Exception:
            self.request_messages = str(messages)
        try:
            meta = [
                {"name": a.get("name"), "mime_type": a.get("mime_type")}
                for a in (attachments or [])
                if isinstance(a, dict)
            ]
            self.attachments_meta = json.dumps(meta, ensure_ascii=False)
        except Exception:
            self.attachments_meta = "[]"
        self.rounds = []
        self.used_provider = None
        self.start_ts = time.time()

    def log_round(self, raw_reply, clean_reply, tool_calls, tool_results_text):
        try:
            calls_json = json.dumps(
                [{"name": n, "args": a} for n, a in (tool_calls or [])],
                ensure_ascii=False,
            )
        except Exception:
            calls_json = "[]"
        self.rounds.append({
            "raw_model_reply": raw_reply if raw_reply is not None else "",
            "clean_reply": clean_reply if clean_reply is not None else "",
            "tool_calls": calls_json,
            "tool_results": tool_results_text or "",
            "entry_kind": "round",
        })

    def log_action(self, name, args=None, result=""):
        try:
            calls_json = json.dumps([{"name": name, "args": args or {}}], ensure_ascii=False)
        except Exception:
            calls_json = json.dumps([{"name": name, "args": {}}], ensure_ascii=False)
        self.rounds.append({
            "raw_model_reply": "",
            "clean_reply": "",
            "tool_calls": calls_json,
            "tool_results": result or "",
            "entry_kind": "action",
        })

    def finish(self, status, final_reply=None, error=None, used_provider=None, auto_detected=False, usage=None):
        duration_ms = int((time.time() - self.start_ts) * 1000)
        created_at = datetime.now(timezone.utc).isoformat()

        u = usage or {}
        tok_in = u.get("input_tokens")
        tok_out = u.get("output_tokens")
        tok_reason = u.get("reasoning_tokens")
        tok_total = u.get("total_tokens")
        try:
            with _logs_db_lock:
                conn = _logs_db_connect()
                try:
                    cur = conn.execute(
                        """INSERT INTO request_logs
                           (created_at, provider, requested_provider, model, auto_detected,
                            status, duration_ms, system_prompt, request_messages,
                            attachments_meta, final_reply, error,
                            input_tokens, output_tokens, reasoning_tokens, total_tokens)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            created_at,
                            used_provider or self.requested_provider,
                            self.requested_provider,
                            self.model,
                            1 if auto_detected else 0,
                            status,
                            duration_ms,
                            self.system_prompt,
                            self.request_messages,
                            self.attachments_meta,
                            final_reply or "",
                            error or "",
                            tok_in, tok_out, tok_reason, tok_total,
                        ),
                    )
                    request_id = cur.lastrowid
                    for idx, r in enumerate(self.rounds):
                        conn.execute(
                            """INSERT INTO request_rounds
                               (request_id, round_index, raw_model_reply, clean_reply, tool_calls, tool_results, entry_kind)
                               VALUES (?,?,?,?,?,?,?)""",
                            (request_id, idx, r["raw_model_reply"], r["clean_reply"], r["tool_calls"], r["tool_results"], r.get("entry_kind", "round")),
                        )
                    conn.commit()
                finally:
                    conn.close()
        except Exception:
            traceback.print_exc()

def get_recent_logs(limit=50, offset=0, provider=None, status=None):
    limit = max(1, min(int(limit or 50), 500))
    offset = max(0, int(offset or 0))

    where_clauses = []
    params = []
    if provider:
        where_clauses.append("provider = ?")
        params.append(provider)
    if status:
        where_clauses.append("status = ?")
        params.append(status)
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    with _logs_db_lock:
        conn = _logs_db_connect()
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""SELECT * FROM request_logs {where_sql}
                    ORDER BY id DESC LIMIT ? OFFSET ?""",
                (*params, limit, offset),
            ).fetchall()
            total = conn.execute(f"SELECT COUNT(*) FROM request_logs {where_sql}", params).fetchone()[0]

            results = []
            for row in rows:
                entry = dict(row)
                round_rows = conn.execute(
                    "SELECT * FROM request_rounds WHERE request_id = ? ORDER BY round_index ASC",
                    (row["id"],),
                ).fetchall()
                rounds = []
                for rr in round_rows:
                    rd = dict(rr)
                    try:
                        rd["tool_calls"] = json.loads(rd.get("tool_calls") or "[]")
                    except Exception:
                        rd["tool_calls"] = []
                    rd["entry_kind"] = rd.get("entry_kind") or "round"
                    rounds.append(rd)
                entry["rounds"] = rounds
                try:
                    entry["attachments_meta"] = json.loads(entry.get("attachments_meta") or "[]")
                except Exception:
                    entry["attachments_meta"] = []
                results.append(entry)
            return results, total
        finally:
            conn.close()

def clear_logs():
    with _logs_db_lock:
        conn = _logs_db_connect()
        try:
            conn.execute("DELETE FROM request_rounds")
            conn.execute("DELETE FROM request_logs")
            conn.commit()
        finally:
            conn.close()

@app.route("/api/logs", methods=["GET"])
def get_logs():
    limit = request.args.get("limit", 50)
    offset = request.args.get("offset", 0)
    provider_filter = request.args.get("provider") or None
    status_filter = request.args.get("status") or None
    try:
        logs, total = get_recent_logs(limit=limit, offset=offset, provider=provider_filter, status=status_filter)
        return jsonify({"logs": logs, "total": total})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/api/logs", methods=["DELETE"])
def delete_logs():
    try:
        clear_logs()
        return jsonify({"ok": True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
