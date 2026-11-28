#!/usr/bin/env python3
"""Stream E (v4) driver: run a list of prompts as ONE continuous conversation
(same chat_id) against the live backend, polling each task to a terminal
state and snapshotting persisted state after every step.

Extension of the v3 driver (~/workspace/navigator-audit/final/run_replay.py):
  - snapshots notification_outbox + full monitoring_events rows
  - snapshots row counts for EVERY sqlite table + full rows for the known
    watch/job tables, so "settings unchanged" is checkable
  - counts contract log lines (SEMANTIC_CLASSIFY / DETERMINISTIC_ROUTE /
    SEMANTIC_OVERRIDE / DISPATCH_DECISION / PRICE_EXTRACT / INPUT_VERIFY /
    SSRF_PROBE) in the backend log between run start and end (by byte offset)
  - records provider/model from the backend log (never assumes it)

Usage:
  python3 run_replay4.py <prompts.json> <out-prefix> [--timeout 420]
  Env: NAV_BASE (default http://127.0.0.1:8000),
       PRIVAGENT_LOCAL_TOKEN (default final-test-token),
       PRIVAGENT_DB_PATH, NAV_BACKEND_LOG (path to backend log file).
Writes <out-prefix>.jsonl (one record per step) and <out-prefix>.json.
All timestamps UTC; latencies in seconds.
"""
import json, os, re, sqlite3, sys, time, urllib.request

BASE = os.environ.get("NAV_BASE", "http://127.0.0.1:8000")
TOKEN = os.environ.get("PRIVAGENT_LOCAL_TOKEN", "final-test-token")
DB = os.environ.get("PRIVAGENT_DB_PATH",
                    os.path.expanduser("~/workspace/navigator-audit/final4/data/privagent.db"))
BACKEND_LOG = os.environ.get("NAV_BACKEND_LOG", "")
TERMINAL = {"completed", "partially_completed", "blocked", "failed",
            "cancelled", "awaiting_input", "awaiting_approval", "interrupted"}
CONTRACT_RES = {
    "SEMANTIC_CLASSIFY": re.compile(r"SEMANTIC_CLASSIFY "),
    "SEMANTIC_CLASSIFY_RETRY": re.compile(r"SEMANTIC_CLASSIFY_RETRY"),
    "SEMANTIC_OVERRIDE": re.compile(r"SEMANTIC_OVERRIDE "),
    "DETERMINISTIC_ROUTE": re.compile(r"DETERMINISTIC_ROUTE "),
    "DISPATCH_DECISION": re.compile(r"DISPATCH_DECISION "),
    "PRICE_EXTRACT": re.compile(r"PRICE_EXTRACT "),
    "INPUT_VERIFY": re.compile(r"INPUT_VERIFY "),
    "SSRF_PROBE": re.compile(r"SSRF_PROBE "),
    "model_mock": re.compile(r"model=mock|MOCK_GREETING_MISMATCH|MockProvider"),
}


def req(method, path, body=None):
    r = urllib.request.Request(BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode(errors="replace")[:500]}
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def db_snapshot():
    out = {}
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        out["tables"] = {}
        for tbl in tables:
            try:
                n = con.execute(f"SELECT COUNT(*) FROM \"{tbl}\"").fetchone()[0]
                out["tables"][tbl] = n
            except sqlite3.Error as e:
                out["tables"][tbl] = {"error": str(e)}
        for tbl, cols in [
            ("monitoring_missions", "id,title,target_url,trigger_type,interval_seconds,condition_type,condition_params,status,next_run_at,last_run_at,created_at,last_state_json"),
            ("email_monitoring_missions", "id,account_email,status,frequency_seconds,created_at"),
            ("monitoring_observations", "id,mission_id,observed_at,observed_value"),
            ("monitoring_events", "id,mission_id,detected_at,notification_sent,delivered_at,acknowledged"),
            ("notification_outbox", "id,kind,ref_id,title,summary,created_at,delivered_at,acknowledged"),
        ]:
            try:
                order = "created_at DESC" if tbl != "monitoring_observations" else "observed_at DESC"
                if tbl == "monitoring_events":
                    order = "detected_at DESC"
                rows = [dict(r) for r in con.execute(f"SELECT {cols} FROM \"{tbl}\" ORDER BY {order}")]
                out[tbl] = rows
            except sqlite3.Error as e:
                out[tbl] = {"error": str(e)}
        try:
            out["scheduled_jobs"] = [dict(r) for r in con.execute("SELECT * FROM scheduled_jobs ORDER BY created_at DESC")]
        except sqlite3.Error as e:
            out["scheduled_jobs"] = {"error": str(e)}
        # Any settings-like tables, full rows (small by construction).
        for tbl in tables:
            if "setting" in tbl.lower() or "config" in tbl.lower() or "preference" in tbl.lower():
                try:
                    out[f"settings:{tbl}"] = [dict(r) for r in con.execute(f"SELECT * FROM \"{tbl}\"")]
                except sqlite3.Error as e:
                    out[f"settings:{tbl}"] = {"error": str(e)}
        con.close()
    except Exception as e:
        out["error"] = str(e)
    return out


def count_log_lines(path, start_offset):
    """Count contract lines appended to the backend log since start_offset.
    Returns (counts dict, new_offset, excerpts dict with up to 3 lines each)."""
    counts = {k: 0 for k in CONTRACT_RES}
    excerpts = {k: [] for k in CONTRACT_RES}
    if not path or not os.path.exists(path):
        return counts, start_offset, excerpts
    with open(path, "rb") as f:
        f.seek(start_offset)
        data = f.read()
        end = f.tell()
    text = data.decode(errors="replace")
    for line in text.splitlines():
        for k, rx in CONTRACT_RES.items():
            if rx.search(line):
                counts[k] += 1
                if len(excerpts[k]) < 3:
                    excerpts[k].append(line[:400])
    return counts, end, excerpts


def log_offset(path):
    try:
        return os.path.getsize(path) if path and os.path.exists(path) else 0
    except OSError:
        return 0


def wait_task(task_id, timeout):
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout:
        st, task = req("GET", f"/api/task/{task_id}")
        last = task if st == 200 else {"http": st, "body": task}
        if last.get("taskState") in TERMINAL:
            return last, time.time() - t0
        time.sleep(5)
    return {**last, "timed_out_waiting": True}, time.time() - t0


def main():
    prompts = json.load(open(sys.argv[1]))
    prefix = sys.argv[2]
    timeout = float(sys.argv[3]) if len(sys.argv) > 3 else 420
    log_start = log_offset(BACKEND_LOG)
    st, chat = req("POST", "/api/chat", {"title": f"E4 {prefix} {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}"})
    assert st in (200, 201), chat
    chat_id = chat.get("chatId") or chat.get("chat_id") or chat.get("id")
    print(f"chat_id={chat_id}", flush=True)
    results = []
    with open(prefix + ".jsonl", "w") as log:
        for p in prompts:
            step_log_start = log_offset(BACKEND_LOG)
            t0 = time.time()
            st, created = req("POST", f"/api/chat/{chat_id}/message", {"text": p["text"]})
            rec = {"n": p["n"], "prompt": p["text"], "chat_id": chat_id,
                   "create_http": st, "create": created,
                   "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0))}
            if st == 200 and created.get("taskId"):
                task, wait_s = wait_task(created["taskId"], timeout)
                rec["task"] = task
                rec["wait_seconds"] = round(wait_s, 1)
                rec["task_id"] = created.get("taskId")
            rec["elapsed_seconds"] = round(time.time() - t0, 1)
            st2, missions = req("GET", "/api/control/missions")
            rec["missions_http"] = st2
            rec["missions"] = missions if st2 == 200 else missions
            st3, email_m = req("GET", "/api/email/missions")
            rec["email_missions_http"] = st3
            rec["email_missions"] = email_m if st3 == 200 else email_m
            st4, jobs = req("GET", "/api/scheduled")
            rec["scheduled_http"] = st4
            rec["scheduled"] = jobs if st4 == 200 else jobs
            st5, events = req("GET", "/api/monitoring/events?limit=100")
            rec["events_http"] = st5
            rec["events_pull"] = events if st5 == 200 else events
            counts, _, excerpts = count_log_lines(BACKEND_LOG, step_log_start)
            rec["log_lines_step"] = counts
            rec["log_excerpts_step"] = excerpts
            rec["db"] = db_snapshot()
            log.write(json.dumps(rec) + "\n"); log.flush()
            results.append(rec)
            print(f"step {p['n']}: state={rec.get('task',{}).get('taskState')} "
                  f"steps={rec.get('task',{}).get('stepsDone')} wait={rec.get('wait_seconds')}s "
                  f"msg={(rec.get('task',{}).get('message') or '')[:160]}", flush=True)
    total_counts, _, total_excerpts = count_log_lines(BACKEND_LOG, log_start)
    json.dump({"chat_id": chat_id, "steps": results,
               "log_lines_total": total_counts,
               "log_excerpts_total": total_excerpts},
              open(prefix + ".json", "w"), indent=2)
    print("DONE chat", chat_id, "log_lines_total=", total_counts)


if __name__ == "__main__":
    main()
