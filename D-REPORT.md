# Stream D — Ask-User + False Completion + Workflow + Reliability

Repo `~/workspace/navigator-audit/PRIVAGENT`, base `0cae209`. Worktree
`~/workspace/navigator-audit/worktrees3/stream-D`. No push. Headline:
**the hard bar passes live** — "Plan a trip to Bengaluru." pauses,
answers, resumes the same task, and reports `blocked` (not failed)
when Chrome is disconnected; the reminder path creates a real scheduled
job with a receipt and never claims a calendar write; a 2-minute
reminder fired live with an outbox notification receipt; a stored DEL
preference was offered back in a new conversation after a restart.

Provider for all live runs: **FreePi `http://127.0.0.1:8452/v1`,
model `deepseek-v4.1-flash`** (real provider, no mock). Live backend:
isolated copy of this worktree's backend at
`~/workspace/navigator-audit/live/streamD-backend/` (fresh venv from
`backend/requirements.txt`, own DB, port 8003, scheduler tick 10 s).
No Chrome/extension was connected in any live run — browser execution
is therefore `blocked`, honestly, in every browser-path case.

## 1. Fixes

### 1.1 Ask-user gate before the extension check (DEMO2 §11a/§11b)

- `backend/app/agentLoop.py:3145` `_detect_trip_missing_info()` —
  deterministic trip/transport missing-slot detector (plan/book +
  trip/flight/train/bus, no date present). Returns a `date_time`
  question asking dates + origin (+ optional budget); offers a saved
  home origin as a confirmable default, never assumed.
- `backend/app/agentLoop.py:3184` `_ask_missing_info()` — shared
  helper: sensitive check → checkpoint save → `_AskUserGate.ask` →
  on answer appends `[Operator's answer to '...': ...]` to the
  instruction and resumes the SAME task; on cancel/timeout/refused
  records a terminal state.
- Browser path (same file, section 3): the missing-info gate
  (`fast_path.detect_missing_info` OR `_detect_trip_missing_info`)
  now runs **before** `real_hub.extension_sockets` is checked. On
  answer, execution falls through to the extension check.
- Extension-disconnected gate now sets state **`"blocked"`** with
  message "Blocked: the Chrome extension is not connected, so this
  browser task cannot start. Connect the extension and retry — the
  task itself did not fail, and nothing was attempted in the browser."
  Mid-task disconnect maps `EXTENSION_DISCONNECTED` in the
  browser-loop exception handler to `"blocked"` too (the session
  status in `session_manager` still records `"failed"` — see §5).
- Conversational branch: trip detector OR'd into the existing missing
  check, so the non-browser variant also pauses instead of completing
  with prose questions.
- `backend/app/contracts.py:34,45` — `"blocked"` added to
  `TASK_STATES` (terminal outcome: run stopped on a missing
  dependency, distinct from `"failed"`).

### 1.2 Reminder / calendar false-completion fix (DEMO2 §11d)

- `backend/app/agentLoop.py:3274` `REMINDER_JOB_PREFIX =
  "[scheduled-reminder] "`.
- `agentLoop.py:3293` `_parse_reminder_time()` — "in N min/hours",
  "tomorrow/today [at] H[:MM] [am|pm]", "at H tomorrow/today", bare
  tomorrow/today (= 09:00 IST), "daily|every day [at] H" → daily UTC
  `HH:MM` (IST − 5:30). All times are the operator's IST.
- `agentLoop.py:3374` `_parse_reminder()` — splits WHAT + WHEN; None
  = not a reminder or no time yet. **Live bug found and fixed during
  verification (see §3.2):** the parser used to run on the full
  instruction including the appended `[Operator's answer to '...in 30
  minutes...': in 2 minutes]` bracket and matched the question's
  example time first. It now parses the time from the operator's
  answer alone when the bracket is present.
- `agentLoop.py:3410` `_create_reminder_job()` — creates a real once/
  daily job via `scheduler.create_job` with the marker instruction.
  The completion message carries the job id + next-run IST and says
  explicitly: "No calendar event was created: PrivAgent has no
  calendar account connected here, so this reminder lives as a
  scheduled job and will arrive as a notification when it fires.
  Receipt: scheduled job `<id>`." No calendar claim anywhere.
- `agentLoop.py:3467` `_fire_scheduled_reminder()` — the marker job
  instruction is intercepted at the top of `run_task`, posts an
  outbox notification (`monitoring_scheduler.post_outbox_notification`
  + broadcast) and completes with "Reminder delivered: … Notification
  receipt `<note_id>`." It never re-asks for the time. The DB path is
  passed explicitly because `monitoring_scheduler`'s schema cache is
  keyed by the `db_path` argument (`None`), which would skip creating
  the outbox table after another database was used in-process.
- Reminder creation is deterministic and never calls the provider
  (pinned by test `test_reminder_with_time_never_calls_provider`).

### 1.3 Workflow learning — executable procedures only (DEMO2 §12)

- `backend/app/long_memory.py:820` `_source_task_completed()` —
  checks `db.get_task_record` state == `"completed"`, else the
  checkpoint `final_state`.
- `long_memory.py:842` `create_executable_skill()` — **refuses to
  save** when `source_task_id` is given but not verifiably completed
  (`ValueError`: "refusing to save an executable skill: source task …
  has no verified completed run on record"). Previously it set
  `verified_runs=1` for any `source_task_id`, which is how DEMO2
  saved a skill from a failed run.
- Skill-v1 payload gains optional `params` (input names; step values
  may contain `{name}`) and `preconditions` (plain-language checks).
  `validate_skill_steps` rejects `{name}` placeholders not in the
  declared params.
- `long_memory.py:960` `extract_skill_inputs()` — deterministic only:
  a param named `url` takes the first http(s) URL; any param takes a
  literal `name: value` / `name=value` pair. Full NL extraction is
  explicitly NOT done (planner work; see §5).
- `long_memory.py:991` `replay_skill_steps(..., inputs=None)` —
  substitutes `{name}` before any action; a missing input fails
  immediately with `fallback: "planner"` and **no action executed**
  (a literal `{name}` is never sent; a stale literal is never
  replayed when the value is a declared parameter).
- `record_skill_verified` preserves `params`/`preconditions` in the
  rebuilt payload (previously dropped them).

### 1.4 Reliability

- `backend/app/main.py:515` `POST /api/chat/{chat_id}/message` —
  now takes `request: Request`, reads `body.text`, falls back to raw
  JSON `text`, and raises **422** ("message text must not be blank —
  send {\"text\": \"...\"} or {\"instruction\": \"...\"}") instead of
  a bare 500. `{"text": "..."}` now creates a task.
- `backend/app/db.py:228` new `task_records` table (task_id PK,
  chat_id, instruction, state, steps_done, message, intent,
  created_at, updated_at); `db.py:858` `save_task_record()` (upsert),
  `db.py:902` `get_task_record()`.
- `backend/app/agentLoop.py` `run_task` wrapper around the renamed
  `_run_task_impl` (+ `_persist_task_record` in `finally`): every
  terminal run writes the `task_records` row AND merges a terminal
  checkpoint (preserves history, sets `working_memory.final_state`
  for completed/failed/cancelled/blocked). Public `run_task` name
  preserved (tests/main monkeypatch it). Never raises; skipped under
  zero retention.
- `extension/src/background.ts:107,122,160` — `surfaceMissionEvent()`
  persists the event to `chrome.storage.session` key `missionEvents`
  (capped at 20) and sets the action badge count (red `#b3261e`).
  `connectProgress` `onmessage` now handles `MISSION_EVENT_TRIGGERED`
  / `NOTIFICATION` / `SCHEDULED_JOB_BLOCKED` instead of dropping
  everything that is not `PROGRESS`. No `notifications` permission
  exists in the manifest, so no OS notification is attempted — badge
  + stored list is the surface.
- `backend/tests/test_long_memory.py` — fixture-only change: the
  skill-proposal checkpoint now carries
  `working_memory={"final_state": "completed"}` (the marker the loop
  writes for a finished run). Assertions unchanged.

### 1.5 Frontend display (scope 8)

`src/agentApp.ts`, `src/taskconsole.ts` — no redesign; existing
surfaces extended where the state was invisible:

- `"blocked"` is terminal everywhere it matters: polling sync
  (`agentApp.ts:377`), timeline completion (`agentApp.ts:1219`),
  WS PROGRESS handler (`agentApp.ts:2201`); `setTaskState` gains a
  `"blocked"` branch (amber `--gate` colours, not danger red);
  completion badge "⛔ Blocked — dependency missing"
  (`agentApp.ts:1306`); `taskconsole.ts` treats blocked as terminal
  (closes stale confirm modal, re-enables Start, hides Cancel) with
  summary "⛔ Blocked — dependency missing"; failed label corrected
  from "✗ Blocked / Failed" to "✗ Failed".
- `awaiting_input` now has UI: WS PROGRESS sets the mission subtext
  to "⏸ Waiting for your answer — reply in this chat to continue the
  same task." (`agentApp.ts`); `taskconsole.ts` summary "⏸ Waiting
  for your answer … Reply in the agent workspace chat."
- Already present and verified in code (not changed): artifacts card
  with Preview/Download, watch cards with status/pause/cancel +
  hydrate, timeline rows with action types, planner-latency pill,
  unread alert badge + `pullMissedEvents` + `renderMonitorAlert`.
- Verification: root `tsc --noEmit` exit 0, `vite build` exit 0.
  Extension `tsc --noEmit` exit 2 with exactly one error in
  `extension/src/actionExecutor.ts:853` (TS2366, missing return) —
  **pre-existing, file untouched by this stream**, identical at HEAD;
  `background.ts` introduces no new tsc errors.

## 2. Tests

New file `backend/tests/test_stream_d_askuser_reliability.py` (17
tests, all passing):

- Pure: trip detection (DEL-aware question; dated trip → None; book
  train no date → asks); reminder parse (once/daily/unparseable/
  no-time); **answer-not-question regression test** (question
  containing "in 30 minutes" + answer "in 2 minutes" → 2 minutes).
- Skill gate: save from a failed run raises `ValueError`; from a
  completed run succeeds with params round-trip; undeclared
  `{placeholder}` rejected. Replay with injected fake
  `send_action`/`observe`: missing input → 0 actions, fallback
  planner, 0 planner calls; supplied inputs → substituted values
  execute, 2 steps, 0 planner calls.
- API: `{"text": "hi"}` → 200 + taskId + task completes with a
  greeting; blank/`{}`/`{"text": 123}` → 422, never 500;
  **restart survival**: completed greeting task still readable via
  `GET /api/task/{id}` after `hub.tasks.clear()`.
- `run_task` end to end: trip → `awaiting_input` → answer →
  same task id → `blocked` + extension message + durable
  `task_records`/`checkpoint` rows; "Open google.com" → `blocked`
  directly; reminder no-time → pause → answer "tomorrow 9am" →
  `completed` with job id + "No calendar event" and no "Google
  Calendar" claim; reminder with time → `completed`, provider never
  called; fired marker job → `completed` + "Notification receipt"
  + unread reminder event in the outbox.

Results:

- `test_stream_d_askuser_reliability.py`: **17 passed**.
- Affected files (`test_chat_api`, `test_contracts`, `test_db`,
  `test_agentLoop`, `test_completion_guard`, `test_long_memory`,
  `test_stream_d_askuser_reliability`): **121 passed**.
- Full suite baseline on this base (before changes): **4 failed,
  1382 passed, 9 skipped** (brief said 5; actual count is 4 — the
  failures are `test_continuous_monitoring::
  test_scheduler_dispatches_task_record_then_detects_change`,
  `test_deepai_gateway::test_gateway_auth_failure_handling` (DeepAI
  gateway down: connection refused), and two
  `test_natural_narration_and_companion` tests).
- Full suite after changes: **4 failed, 1399 passed, 9 skipped**
  (final run, see `/tmp/streamD-full-final.txt`). The 4 failures are
  exactly the pre-existing baseline set. One failure appeared
  mid-work (`test_scheduler::
  test_execute_job_creates_background_task_with_allow_list`) because
  an extension-disconnected browser run now settles as `"blocked"`
  instead of `"failed"` — the intended new vocabulary. The test's
  assertion was updated to accept `"blocked"` first (not weakened:
  it still asserts the job ran as a normal gated task and settled);
  `tests/test_scheduler.py` now passes 10/10.

## 3. Live verification (isolated backend, real provider, no mock)

Provider: FreePi `http://127.0.0.1:8452/v1`, model
`deepseek-v4.1-flash`. Backend: `live/streamD-backend/` on port
8003, fresh venv, own DB, scheduler tick 10 s. Transcripts:
`live/streamD-backend/driver1.log`, `driver2.log`, `driver3.log`.

### 3.1 "Plan a trip to Bengaluru." — VERIFIED LIVE END-TO-END

- Prompt (exact): `Plan a trip to Bengaluru.` via
  `POST /api/chat/{id}/message` with `{"text": ...}`.
- Task `f6b9c9feb2dc4aafb189c0873baa73a5`. Pause after **1.4 s**:
  state `awaiting_input`, message "Which dates are you travelling,
  and from where? For example 'Delhi, 12–14 Dec' — add a budget too
  if you have one, e.g. 'under ₹10,000'."
- `POST /api/task/{id}/ask-user/answer` `{"answer": "Delhi, 12-14
  Dec, under 10000"}` → 200 `{"ok": true}`. Same task id resumes
  and reaches terminal state in **0.0 s**: `blocked`, message
  "Blocked: the Chrome extension is not connected, so this browser
  task cannot start. Connect the extension and retry — the task
  itself did not fail, and nothing was attempted in the browser."
  Total 1.7 s. 0 browser actions (extension never connected).

### 3.2 Reminder — VERIFIED LIVE END-TO-END (including a real bug found by the live run)

- Prompt: `Remind me to call mom` → pause after 1.0 s
  (`awaiting_input`, "When should I remind you? For example
  'tomorrow 9am' or 'in 30 minutes'.").
- **First live run (bug):** answer `"in 2 minutes"` produced a
  **30-minute** job (`182b8d3d4adf4d29be0d16bb5fde63bd`, "in 30
  minutes", next run 09:50 IST) — the parser matched the example
  time inside the question text in the appended answer bracket.
  The task still completed with a real job receipt, but the time
  was wrong, and no notification fired at 2 minutes (0 unread
  events at 09:22). Root cause fixed in `agentLoop.py:3374`
  (parse the operator's answer alone), regression test added.
- **Second live run (fixed):** task
  `8c0255a37f7c4c24888e788bf6e7c612`, answer `"in 2 minutes"` →
  `completed`, message "Reminder set: call mom — in 2 minutes
  (one-off scheduled job `fde7d3b3ccba4ddf979d8f298c40ce6b`, next run
  27 Nov 2026 09:26 IST). No calendar event was created: PrivAgent
  has no calendar account connected here, so this reminder lives as
  a scheduled job and will arrive as a notification when it fires.
  Receipt: scheduled job `fde7d3b3…`."
- **Fire (live):** at 09:26 IST the scheduler ran job
  `fde7d3b3…` (last_task_id `85dfe53fad434d4fb58c1152ea26c075`,
  last_outcome `completed`). Outbox event
  `e7ca7d03-344f-4727-96a2-088740c1857b`, kind `reminder`, title
  "Reminder: call mom", summary "Reminder: call mom — due now.",
  1 unread event via `GET /api/monitoring/events?unread=true`.
  DB `task_records` for the fired task: "Reminder delivered: call
  mom. Notification receipt
  `e7ca7d03-344f-4727-96a2-088740c1857b`." No calendar claim
  anywhere; 0 calendar tables involved.

### 3.3 Preference — VERIFIED LIVE END-TO-END (with one honest gap)

- Store attempt 1 (conversational, real provider): "My home
  airport is Delhi (DEL). Please remember that." → task
  `925a4843b94c4caba74f530e68b0eba4` completed in 5.1 s, model
  answered "Got it — noted and locked in: your home airport is
  Delhi (DEL)…". **But the DB showed 0 rows in
  `user_semantic_facts`, `user_preferences`, `long_memory_facts`
  and `chat_memory` — the model's "remembered" claim was prose
  only; no durable write happened on that path.** (Out of this
  stream's files to fix the model-side store; recorded here.)
- Store attempt 2 (system mechanism): `POST /api/preferences`
  `{"home_airport": "DEL"}` → 200 `{"home_airport": "DEL"}`.
- Backend restarted. `GET /api/preferences` → `{"home_airport":
  "DEL"}` (survived restart).
- New conversation, prompt `Plan a trip to Bengaluru.` → task
  `22e8e515bd7f4e06ab646fb6a7720510`, `awaiting_input` after 3.3 s:
  "Which dates are you travelling, and from where? **I have DEL
  saved as your home airport — should I start from there?** For
  example 'Delhi, 12–14 Dec'…" — the preference is used
  meaningfully (offered as the origin to confirm), not just
  repeated.

### 3.4 API + restart survival — VERIFIED LIVE END-TO-END (one label defect)

- `{"text": ...}` messages return 200 + `taskId` in every live run
  above; blank text returns 422 (unit-tested; live server returns
  the same handler).
- After the restart, `GET /api/task/{id}` for all 5 pre-restart
  tasks returned 200 with messages intact:
  - `3d931e0a…` (reminder) → `completed` ✓
  - `8c0255a3…` (reminder) → `completed` ✓
  - `85dfe53f…` (fired reminder) → `completed` ✓ (DB message is
    the full receipt; the GET message field shows the generic
    "Task completed." — see §5)
  - `925a4843…` (preference) → `completed` ✓
  - `f6b9c9fe…` (trip) → **`interrupted`, not `blocked`** — the
    record survives, the message is preserved, but `main.py:_require`
    only trusts `final_state` in (completed, failed, cancelled), so
    a `blocked` task restores as `interrupted`. DB `task_records`
    still says `blocked`. See §5.

### 3.5 Workflow — VERIFIED COMPONENT ONLY (browser part BLOCKED BY ENVIRONMENT)

- Parameterized replay + save gate: unit-tested with injected fake
  browser (different inputs → different actions, 0 planner calls;
  missing input → fails fast, 0 actions). Live DB shows
  **0 rows in `long_memory_skills`** — nothing was saved from the
  blocked/failed live runs, which is the gate holding in practice.
- No live research+PDF A/B was run in this stream: a real browser
  workflow needs Chrome/extension, which does not exist in this
  environment, and the module-level PDF salvage would only re-prove
  DEMO2's 0→0 planner calls / 5.47 s→6.04 s. **Cut, stated plainly:**
  no measured live workflow speed-up is claimed. The DEMO2 §12
  finding (no improvement) stands unchallenged.
- No live skill was saved end-to-end via the agent path in this
  stream (there is no HTTP skill-creation endpoint in this build;
  skills are created in-process via proposal/confirm).

### 3.6 Frontend / extension display — VERIFIED COMPONENT ONLY

- Root `tsc --noEmit` 0 errors, `vite build` 0 errors; extension
  `background.ts` introduces no new tsc errors (one pre-existing
  error in untouched `actionExecutor.ts:853`). The
  `MISSION_EVENT_TRIGGERED` badge + session-storage surface is
  code-level only — **no live Chrome run was possible from this
  subagent**, so the badge/storage behaviour on a real extension
  is unproven and needs the coordinator's live-browser check.
- The live outbox event in §3.2 is exactly the event the extension
  surface consumes (`MISSION_EVENT_TRIGGERED` via `/ws/client`).

## 4. Commits

Worktree `~/workspace/navigator-audit/worktrees3/stream-D`, messages
prefixed `[D-askuser-reliability]`:

- `1a59956` — Ask-user gate before extension check; blocked state;
  deterministic reminders with job receipts; task_records
  persistence; `{text}` chat fix (`agentLoop.py`, `contracts.py`,
  `db.py`, `main.py`)
- `50328a3` — Verified-only executable skills; parameterized replay
  with fail-fast inputs (`long_memory.py`, `test_long_memory.py`
  fixture)
- `a8c0321` — Extension surfaces `MISSION_EVENT_TRIGGERED`;
  frontend shows blocked/awaiting_input, progress, notifications,
  artifacts (`background.ts`, `agentApp.ts`, `taskconsole.ts`)
- _(this commit)_ — Stream D tests + scheduler assertion for the
  new `blocked` vocabulary + this report
  (`test_stream_d_askuser_reliability.py`, `test_scheduler.py`,
  `D-REPORT.md`)

## 5. Remaining defects / open items

1. **`main.py:_require` degrades `"blocked"` to `"interrupted"` on
   restart** (live-verified §3.4). The durable `task_records` row
   keeps `blocked`; only the GET label is wrong. One-line fix in
   `main.py` (add `"blocked"` to the trusted `final_state` set) —
   outside this stream's `main.py` scope (ChatMessageInput only),
   left for the merge owner.
2. **Same `_require` message fallback:** the fired-reminder task
   reads back as "Task completed." via GET while the DB row holds
   the full receipt. Same area as (1).
3. **Conversational "remember that" stores nothing durably**
   (§3.3): the model says it remembered, but no fact/preference row
   is written. The deterministic `POST /api/preferences` path works
   and is what the live preference proof uses.
4. **Session status still `"failed"` on mid-task disconnect**
   (`session_manager` vocabulary not extended; task state is
   `"blocked"`).
5. **TS `TaskState` union** (`extension/src/contracts.ts`, not
   owned) does not list `"blocked"` yet; runtime-safe (frontend
   treats state as a string).
6. **Parameter extraction from NL is deterministic-only** (`url` /
   `name: value`); full NL extraction is planner work, not done.
7. **NL research via chat remains blocked at the extension gate**
   (`intent_router.py` not owned) — out of scope.
8. **4 pre-existing full-suite failures** (see §2) — none in this
   stream's files; not fixed, not weakened. The scheduler assertion
   updated for the new `blocked` vocabulary is a consequence of the
   intended behaviour change, not a weakening (it still asserts the
   job ran as a normal gated task and settled).
9. **Workflow live A/B**: cut (see §3.5); browser workflows need
    Chrome on the operator's machine.
10. **Live-browser check of the frontend/extension surfaces**
    (scope 8): not possible from this subagent; needs the
    coordinator's live-browser verification against the final
    merged build.
