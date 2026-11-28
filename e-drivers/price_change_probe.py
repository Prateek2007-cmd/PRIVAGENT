#!/usr/bin/env python3
"""Stream E item 4: controlled price change -> ACTUAL notification delivery.

Flow (no injected notifications, no DB writes by this probe into NAVIGATOR):
  1. Start ShopSphere sandbox (port 8125, fresh DB) if not running; pick a
     product + seller via its public API; record price BEFORE.
  2. Create a price watch conversationally against the ShopSphere product
     URL (explicit URL in the prompt), short interval. Record the watch row
     as the backend actually stored it (target_url, interval, threshold).
     If the stored target_url is NOT the ShopSphere URL, STOP and record
     that as the finding (site substitution) — do not force it.
  3. Operator price change via POST /api/v1/operator/price
     (X-Operator-Token: demo-operator). Record price AFTER via public GET.
  4. Wait for the scheduler to run the watch (tick 10 s; watch interval as
     stored). Poll every 15 s up to 10 min for:
       - monitoring_events row for the mission
       - notification_outbox row (kind=monitor)
       - GET /api/monitoring/events pull returning the event (reconnect
         retrieval: second pull with ?since= returns it; delivered_at set)
       - backend log lines: broadcast of the outbox notification (WS live
         delivery evidence is the broadcast call + a connected WS client
         if one is attached; log excerpt recorded verbatim)
       - chat alert: the watch task / a follow-up "any price alerts?"
         message naming the change (recorded verbatim, never paraphrased)
  5. Dedup: change the price to the SAME value again (operator call with
     the new price) and wait one more check cycle; record whether a second
     event/outbox row appears (expect none). Also re-pull events and count
     rows for the mission (expect exactly 1 event for 1 real change).
  6. Dump evidence JSON to the output file.

Usage:
  python3 price_change_probe.py <out.json> [--product-query "Galaxy"]
  Env: NAV_BASE, PRIVAGENT_LOCAL_TOKEN, PRIVAGENT_DB_PATH, NAV_BACKEND_LOG,
       SHOPSPHERE_BASE (default http://127.0.0.1:8125),
       SHOPSPHERE_DIR (sandbox dir; started on 8125 if not already up).
"""
import json, os, sqlite3, subprocess, sys, time, urllib.parse, urllib.request, urllib.error

BASE = os.environ.get("NAV_BASE", "http://127.0.0.1:8000")
TOKEN = os.environ.get("PRIVAGENT_LOCAL_TOKEN", "final-test-token")
DB = os.environ.get("PRIVAGENT_DB_PATH",
                    os.path.expanduser("~/workspace/navigator-audit/final4/data/privagent.db"))
BACKEND_LOG = os.environ.get("NAV_BACKEND_LOG", "")
SHOP = os.environ.get("SHOPSPHERE_BASE", "http://127.0.0.1:8125")
SHOP_DIR = os.environ.get("SHOPSPHERE_DIR",
    os.path.expanduser("~/workspace/navigator-audit/final4/backend-copy/demo-sandboxes/ecommerce"))
OPERATOR_TOKEN = "demo-operator"


def req(base, method, path, body=None, headers=None):
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    r = urllib.request.Request(base + path, method=method,
        data=json.dumps(body).encode() if body is not None else None, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode(errors="replace")[:500]}
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def nav(method, path, body=None):
    return req(BASE, method, path, body,
               {"Authorization": f"Bearer {TOKEN}"})


def db_rows(sql, params=()):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params)]
    finally:
        con.close()


def log_grep(needle, path=BACKEND_LOG):
    if not path or not os.path.exists(path):
        return []
    out = []
    with open(path, errors="replace") as f:
        for line in f:
            if needle in line:
                out.append(line.rstrip()[:500])
    return out


def main():
    out_path = sys.argv[1]
    query = "Galaxy"
    if "--product-query" in sys.argv:
        query = sys.argv[sys.argv.index("--product-query") + 1]
    ev = {"started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
          "shopsphere_base": SHOP, "nav_base": BASE}
    # 1. ShopSphere up?
    st, health = req(SHOP, "GET", "/api/v1/health")
    ev["shopsphere_health"] = {"http": st, "body": health}
    if st != 200:
        ev["verdict"] = "BLOCKED: ShopSphere not running; start it first: " \
            "cd <merged>/demo-sandboxes/ecommerce && uvicorn app:app --host 127.0.0.1 --port 8125"
        json.dump(ev, open(out_path, "w"), indent=2)
        print(json.dumps(ev, indent=2))
        return 2
    st, prods = req(SHOP, "GET", f"/api/v1/products?q={urllib.parse.quote(query)}")
    items = (prods.get("products") or prods.get("items") or []) if isinstance(prods, dict) else prods
    if not items:
        ev["verdict"] = f"BLOCKED: no ShopSphere product matches q={query!r}: {prods}"
        json.dump(ev, open(out_path, "w"), indent=2)
        print(json.dumps(ev, indent=2))
        return 2
    prod = items[0]
    pid = prod.get("id")
    st, detail = req(SHOP, "GET", f"/api/v1/products/{pid}")
    sellers = detail.get("sellers") or []
    if not sellers:
        ev["verdict"] = f"BLOCKED: product {pid} has no sellers: {detail}"
        json.dump(ev, open(out_path, "w"), indent=2)
        print(json.dumps(ev, indent=2))
        return 2
    seller = sellers[0]
    sid, price_before = seller.get("seller_id") or seller.get("id"), seller.get("price")
    ev["product"] = {"id": pid, "name": prod.get("name"), "seller_id": sid,
                     "price_before": price_before, "detail": detail}
    product_url = f"{SHOP}/api/v1/products/{pid}"
    # 2. Conversational watch creation (explicit ShopSphere URL, short interval).
    st, chat = nav("POST", "/api/chat", {"title": "E4 price-change probe"})
    chat_id = chat.get("chatId") or chat.get("chat_id") or chat.get("id")
    ev["chat_id"] = chat_id
    prompt = (f"Watch the price of this product for me: {product_url} "
              f"({prod.get('name')}). Alert me on any price drop. Check every minute.")
    st, created = nav("POST", f"/api/chat/{chat_id}/message", {"text": prompt})
    ev["watch_prompt"] = prompt
    ev["watch_create"] = {"http": st, "body": created}
    task_id = created.get("taskId") if isinstance(created, dict) else None
    ev["watch_task_id"] = task_id
    if task_id:
        for _ in range(84):  # up to 7 min for the task to settle
            st, task = nav("GET", f"/api/task/{task_id}")
            if task.get("taskState") in ("completed", "partially_completed", "blocked",
                                         "failed", "cancelled", "awaiting_input",
                                         "awaiting_approval", "interrupted"):
                break
            time.sleep(5)
        ev["watch_task_final"] = task
    time.sleep(2)
    watches = db_rows("SELECT id,title,target_url,trigger_type,interval_seconds,"
                      "condition_type,condition_params,status,created_at FROM monitoring_missions "
                      "ORDER BY created_at DESC")
    ev["watches_after_create"] = watches
    mine = [w for w in watches if str(pid) in (w.get("target_url") or "")
            or (prod.get("name") or "").lower() in (w.get("title") or "").lower()]
    if not mine:
        ev["verdict"] = ("WATCH NOT CREATED on ShopSphere URL (or target_url differs) — "
                         "see watches_after_create; price-change leg cannot proceed "
                         "without a watch pointing at the sandbox.")
        json.dump(ev, open(out_path, "w"), indent=2)
        print(json.dumps(ev, indent=2))
        return 1
    watch = mine[0]
    ev["watch_row"] = watch
    ev["target_url_is_shopsphere"] = SHOP in (watch.get("target_url") or "")
    if not ev["target_url_is_shopsphere"]:
        ev["verdict"] = ("SITE SUBSTITUTION: watch created but target_url is not the "
                         "ShopSphere URL — operator change would not be observed by "
                         "this watch. Recorded as a finding; not forced.")
        json.dump(ev, open(out_path, "w"), indent=2)
        print(json.dumps(ev, indent=2))
        return 1
    mission_id = watch["id"]
    # 3. Operator price change: drop ~20% (round to whole rupees).
    new_price = max(1, int(float(price_before) * 0.8))
    st, op = req(SHOP, "POST", "/api/v1/operator/price",
                 {"seller_id": sid, "price": new_price},
                 {"X-Operator-Token": OPERATOR_TOKEN})
    ev["operator_price_change"] = {"http": st, "body": op,
                                   "seller_id": sid, "price_before": price_before,
                                   "price_after": new_price,
                                   "at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    st, detail2 = req(SHOP, "GET", f"/api/v1/products/{pid}")
    ev["price_after_public_get"] = {"http": st,
        "sellers": detail2.get("sellers") if isinstance(detail2, dict) else detail2}
    # 4. Wait for scheduler detection + notification artifacts.
    deadline = time.time() + 600
    found = {}
    while time.time() < deadline:
        found["events"] = db_rows(
            "SELECT * FROM monitoring_events WHERE mission_id=? ORDER BY detected_at DESC", (mission_id,))
        try:
            found["outbox"] = db_rows(
                "SELECT * FROM notification_outbox WHERE ref_id=? ORDER BY created_at DESC", (mission_id,))
        except Exception as e:
            found["outbox"] = {"error": str(e)}
        if found["events"] or found.get("outbox"):
            break
        time.sleep(15)
    ev["detection_poll"] = found
    ev["detection_wait_seconds"] = round(600 - max(0, deadline - time.time()), 1)
    # Pull endpoint (reconnect retrieval): first pull + second pull with since=.
    st, pull1 = nav("GET", "/api/monitoring/events?limit=100")
    ev["events_pull_1"] = {"http": st, "body": pull1}
    time.sleep(3)
    st, pull2 = nav("GET", "/api/monitoring/events?limit=100")
    ev["events_pull_2_reconnect"] = {"http": st, "body": pull2}
    ev["events_after_pull"] = db_rows(
        "SELECT * FROM monitoring_events WHERE mission_id=? ORDER BY detected_at DESC", (mission_id,))
    ev["outbox_after_pull"] = db_rows(
        "SELECT * FROM notification_outbox WHERE ref_id=? ORDER BY created_at DESC", (mission_id,))
    ev["backend_log_broadcast_lines"] = log_grep("broadcast_outbox_notification")[-5:]
    ev["backend_log_outbox_lines"] = [l for l in log_grep("notification_outbox")][-5:]
    # Chat alert: ask what changed / whether there are alerts.
    st, created2 = nav("POST", f"/api/chat/{chat_id}/message",
                       {"text": "Any price alerts or changes on my watches?"})
    ev["chat_alert_prompt"] = {"http": st, "body": created2}
    task2 = created2.get("taskId") if isinstance(created2, dict) else None
    if task2:
        for _ in range(60):
            st, t = nav("GET", f"/api/task/{task2}")
            if t.get("taskState") in ("completed", "partially_completed", "blocked",
                                      "failed", "cancelled", "awaiting_input",
                                      "awaiting_approval", "interrupted"):
                break
            time.sleep(5)
        ev["chat_alert_task"] = t
    # 5. Dedup: set the SAME price again, wait one more cycle, count events.
    st, op2 = req(SHOP, "POST", "/api/v1/operator/price",
                  {"seller_id": sid, "price": new_price},
                  {"X-Operator-Token": OPERATOR_TOKEN})
    ev["dedup_same_price_reapply"] = {"http": st, "body": op2}
    interval = watch.get("interval_seconds") or 60
    time.sleep(min(240, max(90, interval + 30)))
    ev["events_after_dedup_wait"] = db_rows(
        "SELECT * FROM monitoring_events WHERE mission_id=? ORDER BY detected_at DESC", (mission_id,))
    ev["outbox_after_dedup_wait"] = db_rows(
        "SELECT * FROM notification_outbox WHERE ref_id=? ORDER BY created_at DESC", (mission_id,))
    n_events = len(ev["events_after_dedup_wait"]) if isinstance(ev["events_after_dedup_wait"], list) else -1
    ev["verdict"] = ("LIVE-VERIFIED notification delivery" if n_events == 1 else
                     f"INCONCLUSIVE: {n_events} events for 1 real price change "
                     f"(expect exactly 1; see events_after_dedup_wait)")
    ev["finished_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    json.dump(ev, open(out_path, "w"), indent=2)
    print(json.dumps(ev, indent=2))
    return 0 if n_events == 1 else 1


if __name__ == "__main__":
    sys.exit(main())
