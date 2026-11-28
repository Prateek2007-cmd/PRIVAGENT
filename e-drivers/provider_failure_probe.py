#!/usr/bin/env python3
"""Stream E item 7: force a provider failure and verify it is reported
accurately WITHOUT mock fallback.

Two legs (each on its own scratch backend copy + fresh DB, port 8001 so the
main FreePi verification backend on 8000 is untouched):
  Leg A — dead provider endpoint: ANTSEED_BASE_URL=http://127.0.0.1:9/v1
          (nothing listens; connection refused immediately).
  Leg B — invalid model: ANTSEED_MODEL=invalid-model-xyz (FreePi answers,
          but the model does not exist).
The shared FreePi proxy itself is NOT stopped: stopping it would disturb
other streams' live runs. A dead endpoint is the controlled equivalent and
is recorded as such.

For each leg: start backend, send "hi" (direct-answer path) and a monitor
prompt, poll both tasks to terminal state, and record verbatim:
  - task state + message text (must name the provider error honestly)
  - backend log: any `model=mock` / `MockProvider` / `MOCK_` lines
    (expect ZERO), any SEMANTIC_CLASSIFY lines, and the error text
  - whether a watch was created anyway (expect none) and DB unchanged
    apart from the task rows.
Verdict rules are printed, not assumed: PASS only if the failure is named
and no mock output appears anywhere; anything else is recorded verbatim.

Usage:
  python3 provider_failure_probe.py <out.json>
  Env: FINAL4 (default ~/workspace/navigator-audit/final4),
       MERGED_COPY (default $FINAL4/backend-copy),
       PYTHON_BIN (default $FINAL4/venv/bin/python).
Requires the merged copy + venv to exist (prepared by the runbook).
"""
import json, os, subprocess, sys, time, urllib.request, urllib.error

F4 = os.environ.get("FINAL4", os.path.expanduser("~/workspace/navigator-audit/final4"))
COPY = os.environ.get("MERGED_COPY", os.path.join(F4, "backend-copy"))
PY = os.environ.get("PYTHON_BIN", os.path.join(F4, "venv", "bin", "python"))
PORT = 8001
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "final-test-token"
TERMINAL = {"completed", "partially_completed", "blocked", "failed",
            "cancelled", "awaiting_input", "awaiting_approval", "interrupted"}


def req(method, path, body=None, timeout=60):
    r = urllib.request.Request(BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode(errors="replace")[:500]}
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def run_leg(name, env_extra, out):
    db_path = os.path.join(F4, "data", f"provider-fail-{name}.db")
    for suf in ("", "-wal", "-shm"):
        try:
            os.remove(db_path + suf)
        except OSError:
            pass
    log_path = os.path.join(F4, f"backend-provider-fail-{name}.log")
    env = dict(os.environ)
    env.update({
        "PRIVAGENT_DB_PATH": db_path,
        "PRIVAGENT_SECRETS_DIR": os.path.join(F4, "data", "secrets"),
        "PRIVAGENT_AUDIT_LOG_PATH": os.path.join(F4, "data", "audit.log"),
        "PRIVAGENT_AGENT_PROFILE_DIR": os.path.join(F4, "data", "agent-profile"),
        "PRIVAGENT_LOCAL_TOKEN": TOKEN,
        "NAVIGATOR_PROVIDER": "antseed",
        "ANTSEED_BASE_URL": "http://127.0.0.1:8452/v1",
        "ANTSEED_MODEL": "deepseek-v4.1-flash",
        "ANTSEED_API_KEY": "dummy",
        "NODE_USE_ENV_PROXY": "1",
        "PRIVAGENT_SCHEDULER_TICK_S": "10",
    })
    env.update(env_extra)
    env.pop("GEMINI_API_KEY", None)
    env.pop("GROQ_API_KEY", None)
    env.pop("DEEPAI_GATEWAY_KEY", None)
    leg = {"name": name, "env_extra": env_extra, "db": db_path, "log": log_path}
    with open(log_path, "w") as lf:
        proc = subprocess.Popen(
            [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(PORT), "--log-level", "info"],
            cwd=COPY, env=env, stdout=lf, stderr=subprocess.STDOUT)
    try:
        up = False
        for _ in range(60):
            st, _ = req("GET", "/api/isolation/status", timeout=5)
            if st == 200:
                up = True
                break
            time.sleep(2)
        leg["backend_up"] = up
        if not up:
            leg["verdict"] = "BLOCKED: scratch backend did not come up; see log"
            out["legs"].append(leg)
            return
        st, chat = req("POST", "/api/chat", {"title": f"E4 provider-fail {name}"})
        chat_id = chat.get("chatId") or chat.get("chat_id") or chat.get("id")
        leg["chat_id"] = chat_id
        leg["turns"] = []
        for prompt in ["hi",
                       "Watch the price of the Sony WH-1000XM5 headphones, check every 20 minutes."]:
            st, created = req("POST", f"/api/chat/{chat_id}/message", {"text": prompt})
            turn = {"prompt": prompt, "create_http": st, "create": created}
            task_id = created.get("taskId") if isinstance(created, dict) else None
            turn["task_id"] = task_id
            if task_id:
                last = {}
                t0 = time.time()
                while time.time() - t0 < 240:
                    st, task = req("GET", f"/api/task/{task_id}")
                    last = task if st == 200 else {"http": st, "body": task}
                    if last.get("taskState") in TERMINAL:
                        break
                    time.sleep(5)
                turn["task_final"] = last
                turn["wait_seconds"] = round(time.time() - t0, 1)
            leg["turns"].append(turn)
        st, missions = req("GET", "/api/control/missions")
        leg["missions_after"] = {"http": st, "body": missions}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
    log_text = open(log_path, errors="replace").read() if os.path.exists(log_path) else ""
    leg["mock_lines"] = [l[:400] for l in log_text.splitlines()
                         if "model=mock" in l or "MockProvider" in l or "MOCK_" in l]
    leg["semantic_lines"] = [l[:400] for l in log_text.splitlines()
                             if "SEMANTIC_CLASSIFY" in l][:10]
    leg["error_lines"] = [l[:400] for l in log_text.splitlines()
                          if ("ERROR" in l or "Traceback" in l or "429" in l
                              or "Connection refused" in l or "404" in l)][:10]
    msgs = " ".join((t.get("task_final") or {}).get("message", "") or ""
                    for t in leg["turns"])
    names_error = any(w in msgs.lower() for w in
                      ("unavailable", "failed", "error", "could not", "couldn't",
                       "provider", "connection", "429", "not available"))
    leg["message_names_provider_error"] = names_error
    leg["verdict"] = (
        "PASS: failure reported honestly, zero mock lines"
        if names_error and not leg["mock_lines"] else
        "FAIL/INCONCLUSIVE: see message text + mock_lines + error_lines")
    out["legs"].append(leg)


def main():
    out_path = sys.argv[1]
    out = {"started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "note": "FreePi proxy itself NOT stopped (shared with other streams); "
                   "dead-endpoint leg is the controlled equivalent.",
           "legs": []}
    run_leg("dead-endpoint", {"ANTSEED_BASE_URL": "http://127.0.0.1:9/v1"}, out)
    time.sleep(3)
    run_leg("invalid-model", {"ANTSEED_MODEL": "invalid-model-xyz"}, out)
    out["finished_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    json.dump(out, open(out_path, "w"), indent=2)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
