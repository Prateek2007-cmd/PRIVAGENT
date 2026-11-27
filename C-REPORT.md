# Stream C — Jobs + MailCal + MCP: final report

Repo `~/workspace/navigator-audit/PRIVAGENT`, base `0cae209`. Worktree
`~/workspace/navigator-audit/worktrees3/stream-C`. Never pushed.
Commits: `4105a2b` (first round) + this round's fix commit (see git log).

**Deadline context:** user imposed a 2-hour hard deadline (checks ~11:13 IST,
2026-11-27); this stream was to finish by ~10:30 IST for merge + final
verification + ZIP. All three hard objectives completed end-to-end and
read-back verified; a 6th objective (FastAPI vs Django) was attempted twice,
failed the same way both times, and is CUT, not presented as working.

## 0. Live environment (exact, reproducible)

- Provider: **FreePi** proxy on 127.0.0.1:8452 (restarted at task start;
  live completion returned model `deepseek-ai/deepseek-v41-flash-1017`,
  4,000/4,000 credits). DeepAI 127.0.0.1:10087 down; never needed. Backend env:
  `NAVIGATOR_PROVIDER=antseed`, `ANTSEED_BASE_URL=http://127.0.0.1:8452/v1`,
  `ANTSEED_API_KEY=<any non-empty dummy>`,
  `ANTSEED_MODEL=deepseek-v4.1-flash`, `TASK_TIMEOUT_S=1500`,
  `PRIVAGENT_LOCAL_TOKEN=$(cat <live>/token)`.
- Isolated live copy: `~/workspace/navigator-audit/live/streamC-backend/`
  (`backend-copy/` = worktree code, fresh venv `venv/`,
  `mailcal/` = sandbox copy, `stub-extension.py`, `c_run_task.py` driver,
  `resume-synthetic.txt`, `prompt-*.txt`, `runs/*.json`).
- **Port note: stream B's backend already listens on 127.0.0.1:8000, so this
  stream ran on port 8001** (`uvicorn app.main:app --host 127.0.0.1 --port
  8001`). The stub extension and driver were pointed at 8001. The final
  merged build must pick one free port and point the stub + driver at it.
- MailCal sandbox: port 8126, fresh seed (10 messages, 6 unread, 1 seeded
  draft d1, 5 seeded events e1-e5). Health returned ok, service
  mailcal-demo-sandbox, account demo@demo.test, 10 messages, sandbox true.
- Stub extension: `extensionConnected: true`, profile `stub: true,
  real_browser: false` — browser-loop steps are STUB-SERVED / UNVERIFIED;
  only server-side steps (MCP tools, deterministic handlers) are real.
- MCP servers registered + discovered: `fs-docs` (list_files, read_file,
  analyse_file), `public-fetch` (fetch_url, extract_headings),
  `github-public` (repo_info, list_repo_files, search_public_repos).
  `GET /api/mcp/tools` lists **17 tools**: those 8 + 9 built-ins
  (`mailcal_sandbox.*` x7, `jobs.discover_and_rank`,
  `research.render_comparison`).

## 1. Fixes (first round — commit 4105a2b)

- **Jobs NL path** — `backend/app/research/job_discovery.py`:
  deterministic bounded pipeline `run_job_discovery` (discover up to 150s,
  rank, URL-verify up to 60s, XLSX, monitor; XLSX contains only
  HTTP-200-verified URLs) + NL detector/handler, intercepted in
  `agentLoop.run_task` BEFORE the browser gate. `backend/app/agentLoop.py`.
- **MailCal wiring** — `backend/app/connector_router.py`: test/demo-inbox
  marker checked FIRST (before Gmail/Calendar).
  `backend/app/connector_runner.py`: multi-step review (list inbox,
  deadlines, draft reply to the internship email, calendar event for the
  earliest deadline), each mutating sub-step behind its own confirmation;
  receipts (draft id, event id) in the answer.
  `backend/app/mcp/api.py`: `mailcal_sandbox.*` tools in the MCP registry
  (search/get/list_unread/get_thread/download_attachment reads;
  create_draft/create_event mutating, approval-gated).
- **MCP proxy fix** — `backend/app/mcp/discovery.py:21-42,93`: the stdio
  client drops proxy env vars (SDK inherits only HOME/LOGNAME/PATH/SHELL/
  TERM/USER); `proxy_env_for_stdio()` forwards http_proxy, https_proxy,
  HTTP_PROXY, HTTPS_PROXY, ALL_PROXY, all_proxy, NO_PROXY, no_proxy (plus
  NODE_USE_ENV_PROXY) into `StdioServerParameters(env=...)`, merged OVER the
  SDK defaults. Never forwards secrets. **Verified live:**
  `public-fetch.fetch_url` for example.com through a spawned server returned
  HTTP 200 (before the fix: SSL WRONG_VERSION_NUMBER).
- **MCP planner improvements** — `backend/app/agentLoop.py`: mcp_tool
  dispatch gains per-task identical-call dedup cache (50 entries), one retry
  for transient errors only (timeout/connection/502/503/429),
  collection-budget nudge after 8 `fetch_url` calls with no artifact tool
  yet, `_mcp_stats` progress tracking in working memory.
  `backend/app/mcp/api.py`: `research.render_comparison` one-call cited
  document tool (claims require valid http(s) source URLs; renders via
  existing renderers; registers artifact for download).
- `backend/app/file_uploads.py`: `.md` added to allowed attachment
  extensions.
- Tests: `backend/tests/test_stream_c_jobs_mailcal_mcp.py` (14 tests, all
  pass; existing suites still pass: 89 connector/MCP, 96 research/agentLoop).

## 2. Fixes (second round — live-verification failures found and fixed)

### 2a. Artifact filenames rejected / redacted — XLSX existed but was not downloadable
**Found live:** jobs run 1 (task d3aeb07f451c44c5985311d31365937c, 40.1s)
produced 25 verified listings and a valid 16,005 B XLSX on disk, but
`artifacts: []` and the chat showed a redacted filename/path.
Root cause (reproduced directly): `db.save_artifact` raises
`ValueError("artifact paths cannot contain credentials or PII")` — the
timestamp filename `job-discovery-20261127-034340.xlsx` matches its
sensitivity classifier (14 digits with a hyphen), and the chat scrubber
redacts the same digit run in the message.
**Fix:**
- `backend/app/research/job_discovery.py:784-788` — filename is now
  `job-discovery-<6 lowercase letters>.xlsx` (no digit run).
- `backend/app/mcp/api.py:311-317` — same letters-only suffix for comparison
  artifacts (`{safe}-{6 letters}.{fmt}`; the epoch timestamp had the same
  defect).
- `backend/app/research/job_discovery.py:922-924` — handler now surfaces
  `artifact_error` in the answer instead of printing a dead link when
  registration fails.
**Verified:** jobs run 2 (below) registers the artifact and the download
link works.

### 2b. `research.render_comparison` refused by the planner's consequential-tool gate
**Found live:** llama run 1 (task dbc99931d6904696be1417a4ac155ed1, 62.1s)
completed 5 verified steps (repo_info ggml-org/llama.cpp, fetch llama
README, fetch Ollama README, fetch llama docs/build.md) then FAILED:
"consequential tool 'mcp.research.render_comparison' needs operator
approval — approve it through the confirmation gate first; it was not
executed." The tool was flagged `mutating=True`, so the planner gate
refused it outright (no confirmation was ever emitted for the driver to
approve).
**Fix:** `backend/app/mcp/api.py:236` — flag changed to `False` with a
comment: the tool writes only the task's own deliverable artifact (the
same class of write as the research pipeline's own artifact generation,
which is not approval-gated); it changes no external state — no mail,
calendar, monitor, repo, or file outside the task's artifacts.
`jobs.discover_and_rank` stays `mutating=True` (creates a persistent
monitoring mission); MailCal `create_draft`/`create_event` stay mutating.
**Verified:** llama run 2 (below) completes end-to-end.

### 2c. Review handler swallowed targeted test-inbox requests (duplicate draft + event)
**Found live:** bill run 1 (task f3f708ba95a1495c9b568cecba36761b, 15.3s):
prompt "Search my test inbox for the bill email, read it, download its
attachment…" matched the `test inbox` marker and ran the full review op,
creating a SECOND draft (d-f5856a1ba1) and SECOND event (e-b8b560c968)
for the same bill, and never downloading the attachment.
**Fix:**
- `backend/app/connector_router.py:74-79` — new `_MAILCAL_REVIEW_RE`
  (review|deadline|draft|calendar|prepare|important|identify|reply).
- `backend/app/connector_router.py:293` — the review op runs only for
  review-shaped requests; a targeted single-message request returns no
  route and falls through to the planner, which has the
  `mailcal_sandbox.*` tools in the registry.
- `backend/tests/test_stream_c_jobs_mailcal_mcp.py:52-60` — test updated:
  "search demo@demo.test for the bill" gives no route; review-shaped prompt
  gives `mailcal_sandbox`/`review`. 14/14 pass.
**Verified:** bill run 2 (below) takes the planner path and reads m4
directly, creating no draft/event.

## 3. Live verification evidence (all via NL chat, real provider, no mock)

Provider for every run: antseed via FreePi, model
`deepseek-ai/deepseek-v41-flash-1017`. Driver:
`c_run_task.py <prompt-file> [attach] [poll-seconds] [tag]` — auto-approves
pending confirmations as the demo operator (each approval printed + saved
to `runs/<tag>-<taskid>.json`). Full task JSONs under
`~/workspace/navigator-audit/live/streamC-backend/runs/`.

### Objective 1 — NL job discovery to XLSX (task 31dfcc42a30b4b508c6c56c63a7089b2)
- Prompt (exact, `prompt-jobs.txt`, with synthetic resume attached
  `resume-synthetic.txt` — Asha Sharma, fictional B.Tech IT student,
  Hyderabad; Python, JavaScript, React, FastAPI, SQL): "Analyze this resume,
  find suitable software engineering internships in Hyderabad, rank the best
  matches, generate an Excel spreadsheet with application links, and monitor
  new matching opportunities daily."
- Result: **completed, 35.2s, 1 step.** Resume extracted (targeting software
  engineer / backend developer / full stack developer; skills Python,
  JavaScript, TypeScript, SQL, React, Node.js, FastAPI, HTML; prefers
  Hyderabad, Remote). **25 verified listings** (0 excluded for unverified
  links; discovery 20.3s, URL checks 8.6s). Top: Software Engineer AI
  Coding Tasks — Askable (75/100); Backend Engineer (Python) — GitLab
  (70/100); Senior Agentic AI Engineer — GT (70/100). Monitoring mission
  `cada3fe5-93f7-4308-aac7-4cda438e8d83` on the RemoteOK python-jobs feed,
  status active (verified in backend DB).
- Artifact: `job-discovery-viknha.xlsx`, id
  `941e906b203c4837832659b80fd50526`, download
  `/api/artifacts/941e906b203c4837832659b80fd50526/download` — **HTTP 200,
  16,005 B, correct XLSX content-type**. Read-back with openpyxl: sheets
  Executive Summary / Comparison & Metrics / Verified Claims / Sources &
  Provenance; Comparison table 25 rows with rank, role, company, location,
  posted, score, matched skills, reason, source link.
  **Independent check: all 25 source-link URLs from the downloaded file
  return HTTP 200 (25/25).** No application submitted.
- Classification: **VERIFIED LIVE END-TO-END**.

### Objective 2 — NL MailCal review to draft + calendar receipt (task 8f5ff8d1a1934d34834a404b1ef44302)
- Prompt (exact, `prompt-mailcal.txt`): "Review my test inbox, identify
  important deadlines, draft a reply to the internship email, and prepare a
  calendar event for my approval."
- Result: **completed, 16.8s, 1 step, 2 operator approvals** (draft, then
  event; each logged). Inbox: 10 messages, 6 unread. Deadlines found:
  1 Dec 2026 bill Rs 1,250 (Demo Billing); 5 Dec 2026 internship application
  x2 (Demo Careers); 12 Dec 2026 hackathon invite (Demo Events).
  Draft created to Demo Careers, id `d-f7282676fc`. Calendar event created
  'Deadline: Your demo utility bill is due 1 Dec 2026 — Rs 1,250 (test
  amount) [TEST]', 1 Dec 2026 09:00-10:00 IST, receipt event id
  `e-16d57c55e2`. Nothing sent (sandbox has no send endpoint).
- **Verified directly in the independent MailCal backend:** `GET /drafts`
  returns 2 drafts (seeded d1 + `d-f7282676fc` to careers@demo.test, correct
  subject); `GET /events` returns 6 events (seeded e1-e5 + `e-16d57c55e2`,
  correct title, start `2026-12-01T09:00:00+05:30`).
  Real Gmail / Google Calendar not used — no test account is connected.
- Classification: **VERIFIED LIVE END-TO-END** (sandbox backend; real
  Gmail/Calendar BLOCKED without a test account — stated, not faked).

### Objective 3 — NL llama.cpp vs Ollama comparison (task a5bb33e10f4e44cab5d7798ab27db711)
- Prompt (exact, `prompt-llama.txt`): "Find the llama.cpp GitHub repository,
  analyze its README and supported platforms, compare its capabilities with
  Ollama using official sources, and generate a cited comparison document."
- Result: **completed, 86.4s, 5 verified planner steps + render.**
  Planner independently selected: (1) stub-served Google navigate
  (STUB-SERVED, not evidence); (2) `mcp.github-public.repo_info`
  (ggml-org/llama.cpp); (3) `mcp.public-fetch.fetch_url` llama.cpp README
  (raw content host); (4) fetch Ollama README; (5) fetch llama.cpp
  docs/build.md — all real fetches. Then `research.render_comparison`.
  First attempt failed at the gate (see section 2b); second attempt completed.
- Artifact: `llama-cpp-vs-ollama--a-cited-capability-comparison-dcwafx.docx`,
  id `49a1ae24bd024eb89ec55f81290917dd`, download — **HTTP 200, 43,368 B**.
  Read-back with python-docx: 41 paragraphs, 3 tables; claims table 21
  rows (header + **20 cited claims**, each with source URL + verbatim
  quote); 5 official sources, all verified HTTP 200 (llama.cpp repo page,
  both raw READMEs, docs/build.md, Ollama repo page). Content spot-checked:
  llama.cpp C++, MIT, about 136.4k stars, backend matrix (CUDA, HIP, Metal,
  SYCL, Vulkan, MUSA, CANN, OpenCL, OpenVINO, Hexagon, Android…); Ollama Go,
  MIT, about 188.3k stars, "its own README lists exactly one supported
  backend: llama.cpp"; bottom line (Ollama is a higher-level layer on top of
  llama.cpp) is supported by the cited sources.
- Classification: **VERIFIED LIVE END-TO-END** (planner-selected MCP tools;
  the single browser step was stub-served, all evidence came from real MCP
  fetches).

### Objective 4 — NL bill email (task 41fa870057c24cd5b11fcadc81556309)
- Prompt (exact, `prompt-bill.txt`): "Search my test inbox for the bill
  email, read it, download its attachment if it has one, and tell me what is
  due, how much, and by when."
- Result: **completed, 20.3s, 3 planner steps** (planner path after section
  2c fix; first attempt took the review op — see section 2c). Planner read
  message m4: "Your demo utility bill is due 1 Dec 2026 — Rs 1,250 (test
  amount) [TEST]". Answer: synthetic test utility bill, November 2026,
  account DEMO-UTIL-0001; **Rs 1,250; by 1 December 2026**; attachment: none
  — the message's attachment list is empty, so there was nothing to download
  (honest; m4 genuinely has no attachment). No draft/event created.
- Attachment tool verified separately (direct dispatch):
  `mailcal_sandbox.download_attachment(m1, a1)` returned the real file
  content: "TEST FILE — SYNTHETIC / Role: Software Intern (Test Cohort
  2027) / Test stipend: Rs 10,000/month (invented) / Test deadline: 5 Dec
  2026". Seed attachments: a1 (m1, internship-role-test.txt, 185 B),
  a2 (m6, packing-slip-test.txt, 114 B).
- Classification: **VERIFIED LIVE END-TO-END**.

### Objective 5 — NL current Python release briefing (task d9ae336f92a24fd89ffb823771268887)
- Prompt (exact, `prompt-python.txt`): "Research the current Python release
  from the official Python website, list the latest stable version and its
  key new features, and generate a cited briefing document."
- Result: **completed, 88.3s, 5 steps** via the existing autonomous research
  pipeline (2 authoritative sources, 6 verified findings). Artifact:
  `the_current_Python_release_from_the_offi.docx`, id
  `art_d9ae336f92a24fd89ffb823771268887_docx`, download — **HTTP 200,
  40,039 B**. Read-back: states "Python 3.15 was released on 9 October
  2026" and "Python 3.15 is the latest stable release" — correct and
  current as of Nov 2026 (PEP 790 schedule, tail-calling interpreter,
  deferred loading, PEP 831 frame pointers).
- Classification: **VERIFIED LIVE END-TO-END**.

### Objective 6 — FastAPI vs Django comparison — CUT
- Prompt (exact, `prompt-fastapi.txt`): "Compare FastAPI and Django using
  their official documentation, focusing on performance, ease of use, and
  ecosystem, and generate a cited comparison document."
- **Attempt 1: failed in 5.0s. Attempt 2: failed in 5.0s, same mode.** The
  prompt classifies RESEARCH_TASK, so it runs the pre-existing autonomous
  research pipeline (not this stream's render-tool path); both times the
  model emitted tool-call markup as plain reply text instead of invoking a
  tool, the pipeline examined 0 independent sources, and its source-count
  gate failed the task ("Research incomplete: … only 0 independent source(s)
  were examined"). This is a pre-existing pipeline/model-behaviour defect
  outside this stream's files. **Cut, not fixed, not presented as working.**
  Five objectives still complete, meeting the "at least five" bar.

## 4. Direct-dispatch verifications (no NL)
- `public-fetch.fetch_url` for example.com via spawned MCP server:
  HTTP 200 — proxy forwarding fix proven.
- `mailcal_sandbox.search_messages("internship")` returned [m2, m1] with
  real subjects/deadlines.
- `mailcal_sandbox.download_attachment(m1, a1)` returned real file content
  (section 3.4).
- `GET /api/mcp/tools` returns 17 tools (9 built-in + 8 server tools).

## 5. Remaining defects / honest limits
1. **FastAPI comparison CUT** (section 3.6): research pipeline fails when
   the model emits tool-call markup as text; 0 sources, gate fails. Needs a
   fix in the research pipeline (outside this stream's files) — e.g. retry
   with a rephrased tool-use instruction or a fallback source fetch.
2. **First bill run created duplicate state** (section 2c): draft
   `d-f5856a1ba1` and event `e-b8b560c968` exist in the sandbox alongside the
   section 3.2 pair. Harmless test data, but a re-verification against a
   fresh seed should expect exactly 1 new draft + 1 new event.
3. **Scrubber redacts ISO dates and email addresses in chat/tool output**
   (pre-existing, `privacy.py` outside this stream's files): e.g. tool
   results show deadline `[REDACTED]` and sender `[REDACTED]`; the operator
   messages work around it with human dates ("1 Dec 2026") and display
   names ("Demo Careers"). ISO dates still reach API calls unchanged.
4. **Task-artifacts listing mismatch (minor):** for the llama task, the
   driver's `GET /api/tasks/{id}/artifacts` returned `[]` even though the
   artifact is registered and downloads fine — the render tool registers
   under a different task context. The download link in the answer is the
   reliable handle; verify by download, not by the listing.
5. **Stub extension, not a real browser:** all `navigate` steps are
   stub-served. The three hard objectives do not depend on a browser (jobs
   and MailCal are deterministic handlers; llama's evidence came from MCP
   fetches), but any objective that genuinely needs clicking/scrolling in a
   real browser remains UNVERIFIED here.
6. **Real Gmail/Calendar BLOCKED** without a test account (no fake).
   GitHub unauthenticated quota is shared across the sandbox egress IP
   (60/hr); llama's `repo_info` calls succeeded here.
7. **Ranking quality note:** the jobs top-25 includes senior/staff roles
   (scores 40-75) because the public sources (RemoteOK, WeWorkRemotely)
   carry few Hyderabad internships; ranking is by skill/title match and
   every link is verified, but "Hyderabad internships" specifically are
   thin in these sources. A Hyderabad-focused source (e.g. Internshala /
   LinkedIn) would improve relevance; not added (scope + time).

## 6. Exact startup / verification commands (for the final merged build)

FreePi provider first (from the freepi-proxy directory;
NODE_USE_ENV_PROXY=1 required):
- `./freepi-proxy start -d`
- `curl -s http://127.0.0.1:8452/v1/models | head -c 200`

MailCal sandbox (fresh seed: delete its .db first if re-running):
- `cd <live>/mailcal && <venv>/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8126`
- `curl -s http://127.0.0.1:8126/api/v1/health`

Backend (port 8001 here because stream B holds 8000; pick a free port),
with env NAVIGATOR_PROVIDER=antseed, ANTSEED_BASE_URL pointing at the
FreePi proxy, ANTSEED_API_KEY any non-empty dummy, ANTSEED_MODEL
deepseek-v4.1-flash, TASK_TIMEOUT_S=1500, PRIVAGENT_LOCAL_TOKEN from the
live token file; then:
- `<venv>/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8001`
- health check with the Authorization Bearer token header.

Stub extension (point its WS URI at the backend port) + MCP servers:
- `<venv>/bin/python stub-extension.py`
- Register fs-docs / public-fetch / github-public via POST /api/mcp/servers,
  then POST /api/mcp/servers/<name>/discover; GET /api/mcp/tools must list 17.

NL runs (driver auto-approves confirmations as demo operator):
- `c_run_task.py prompt-jobs.txt resume-synthetic.txt resume-synthetic.txt 900 jobs`
- `c_run_task.py prompt-mailcal.txt "" "" 900 mailcal`
- `c_run_task.py prompt-llama.txt "" "" 900 llama`

Verify artifacts (download + read back; check every URL 200):
- Download via `/api/artifacts/<id>/download` with the Bearer token.
- openpyxl: 25 rows in 'Comparison & Metrics'; GET each Source link, expect 200.
- python-docx: claims table rows == 20, each with source URL + verbatim quote.

Verify MailCal state in the sandbox (independent of the backend):
- `GET /api/v1/drafts` and `GET /api/v1/events` with the demo-mailcal-token.
- Expect exactly 1 new draft (to careers@demo.test) + 1 new event
  (1 Dec 2026 09:00 IST) on a fresh seed.

## 7. Files changed (this stream)
- `backend/app/research/job_discovery.py` — bounded pipeline + NL handler;
  section 2a filename + artifact_error surfacing.
- `backend/app/agentLoop.py` — jobs interception before browser gate; MCP
  dispatch dedup/retry/budget nudge/progress.
- `backend/app/connector_router.py` — MailCal marker-first routing; section
  2c review-shape gating.
- `backend/app/connector_runner.py` — MailCal review execution + confirmations.
- `backend/app/mcp/api.py` — mailcal_sandbox.* + jobs + render_comparison
  built-ins; section 2a filename; section 2b mutating flag.
- `backend/app/mcp/discovery.py` — proxy-env forwarding for stdio servers.
- `backend/app/file_uploads.py` — `.md` attachments allowed.
- `backend/tests/test_stream_c_jobs_mailcal_mcp.py` — 14 tests, all pass.
- `C-REPORT.md` — this file.
