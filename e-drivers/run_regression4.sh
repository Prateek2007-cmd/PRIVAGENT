#!/bin/bash
# Stream E item 8: COMPLETE regression on the merged build vs pristine ab003b9.
# Usage: ./run_regression4.sh <merged_copy_dir> <out_prefix>
#   merged_copy_dir : isolated copy of the MERGED commit (backend/ + extension/ + frontend)
#   out_prefix      : e.g. ~/workspace/navigator-audit/final4/regression-v4
# Runs, for BOTH the merged copy and a fresh pristine ab003b9 worktree:
#   1. backend pytest full suite (fresh venv per tree)
#   2. frontend build (npm run build)
#   3. extension build + vitest
# Writes <out_prefix>-merged.{log,json} and <out_prefix>-base.{log,json} plus
# <out_prefix>-diff.txt (named failures present in merged but absent in base).
# Exit 0 only if the script itself ran to completion (regression verdicts are
# in the JSON files, not the exit code).
set -u
MERGED="${1:?usage: run_regression4.sh <merged_copy_dir> <out_prefix>}"
OUT="${2:?usage: run_regression4.sh <merged_copy_dir> <out_prefix>}"
F4="$HOME/workspace/navigator-audit/final4"
REPO="$HOME/workspace/navigator-audit/PRIVAGENT"
BASE_WORKTREE="$F4/base-ab003b9"

run_tree() {
  local DIR="$1" TAG="$2" VENV="$3"
  local LOG="$OUT-$TAG.log" JSON="$OUT-$TAG.json"
  { echo "=== tree: $DIR tag: $TAG at $(date -u +%FT%TZ) ==="; } > "$LOG"
  # --- backend pytest ---
  if [ ! -x "$VENV/bin/python" ]; then
    echo "--- creating venv $VENV" >> "$LOG"
    python3 -m venv "$VENV" >> "$LOG" 2>&1
    "$VENV/bin/pip" install -q -r "$DIR/backend/requirements.txt" >> "$LOG" 2>&1 || echo "PIP INSTALL FAILED (see log)" >> "$LOG"
  fi
  echo "--- backend pytest" >> "$LOG"
  (cd "$DIR/backend" && "$VENV/bin/python" -m pytest -q -p no:cacheprovider > "$OUT-$TAG-pytest.log" 2>&1; echo "pytest exit=$?" >> "$LOG")
  tail -5 "$OUT-$TAG-pytest.log" >> "$LOG"
  echo "--- frontend build" >> "$LOG"
  (cd "$DIR" && npm run build > "$OUT-$TAG-frontend.log" 2>&1; echo "frontend build exit=$?" >> "$LOG")
  tail -5 "$OUT-$TAG-frontend.log" >> "$LOG"
  echo "--- extension build" >> "$LOG"
  (cd "$DIR/extension" && npm run build > "$OUT-$TAG-extbuild.log" 2>&1; echo "extension build exit=$?" >> "$LOG")
  tail -5 "$OUT-$TAG-extbuild.log" >> "$LOG"
  echo "--- extension vitest" >> "$LOG"
  (cd "$DIR/extension" && npx vitest run > "$OUT-$TAG-vitest.log" 2>&1; echo "vitest exit=$?" >> "$LOG")
  tail -5 "$OUT-$TAG-vitest.log" >> "$LOG"
  python3 - "$OUT-$TAG-pytest.log" "$OUT-$TAG-vitest.log" "$OUT-$TAG-frontend.log" "$OUT-$TAG-extbuild.log" "$JSON" <<'PYEOF'
import json, re, sys
pytest_log, vitest_log, fe_log, ext_log, out = sys.argv[1:6]
def parse_pytest(path):
    txt = open(path, errors="replace").read()
    m = re.search(r"(\d+) passed(?:, (\d+) failed)?(?:, (\d+) skipped)?(?:, (\d+) xfailed)?(?:, (\d+) xpassed)?", txt)
    d = {"passed": 0, "failed": 0, "skipped": 0, "xfailed": 0, "xpassed": 0}
    if m:
        for i, k in enumerate(["passed", "failed", "skipped", "xfailed", "xpassed"], 1):
            d[k] = int(m.group(i) or 0)
    # named failures: lines starting with FAILED or "FAILED <nodeid>"
    fails = sorted(set(re.findall(r"FAILED (\S+)", txt)))
    # also short-summary failures
    fails += sorted(set(re.findall(r"FAILED (\S+) - ", txt)))
    d["failed_tests"] = sorted(set(fails))
    d["tail"] = txt.strip().splitlines()[-3:]
    return d
def parse_vitest(path):
    txt = open(path, errors="replace").read()
    m = re.search(r"Test Files\s+(\d+) passed", txt)
    n = re.search(r"Tests\s+(\d+) passed", txt)
    f = re.search(r"Tests\s+.*(\d+) failed", txt)
    return {"test_files_passed": int(m.group(1)) if m else 0,
            "tests_passed": int(n.group(1)) if n else 0,
            "tests_failed": int(f.group(1)) if f else 0,
            "tail": txt.strip().splitlines()[-3:]}
def exit_of(path, marker):
    txt = open(path, errors="replace").read()
    m = re.search(re.escape(marker) + r" exit=(\d+)", txt)
    return int(m.group(1)) if m else None
res = {"pytest": parse_pytest(pytest_log), "vitest": parse_vitest(vitest_log),
       "frontend_build_exit": exit_of(fe_log, "frontend build"),
       "extension_build_exit": exit_of(ext_log, "extension build")}
json.dump(res, open(out, "w"), indent=2)
print(json.dumps(res, indent=2))
PYEOF
  cat "$JSON" >> "$LOG"
}

mkdir -p "$(dirname "$OUT")"
# Pristine base worktree (fresh, detached at ab003b9).
if [ ! -d "$BASE_WORKTREE" ]; then
  git -C "$REPO" worktree add --detach "$BASE_WORKTREE" ab003b9 >> "$OUT-base-setup.log" 2>&1
fi
run_tree "$MERGED" "merged" "$F4/venv"
run_tree "$BASE_WORKTREE" "base" "$F4/venv-base"
python3 - "$OUT-merged.json" "$OUT-base.json" "$OUT-diff.txt" <<'PYEOF'
import json, sys
m = json.load(open(sys.argv[1])); b = json.load(open(sys.argv[2]))
mf = set(m["pytest"]["failed_tests"]); bf = set(b["pytest"]["failed_tests"])
diff = {"merged_only_failures": sorted(mf - bf),
        "base_only_failures": sorted(bf - mf),
        "common_failures": sorted(mf & bf),
        "merged_pytest": m["pytest"], "base_pytest": b["pytest"],
        "merged_vitest": m["vitest"], "base_vitest": b["vitest"],
        "merged_frontend_exit": m["frontend_build_exit"],
        "base_frontend_exit": b["frontend_build_exit"],
        "merged_extbuild_exit": m["extension_build_exit"],
        "base_extbuild_exit": b["extension_build_exit"]}
open(sys.argv[3], "w").write(json.dumps(diff, indent=2))
print(json.dumps(diff, indent=2))
PYEOF
echo "DONE. See $OUT-merged.json $OUT-base.json $OUT-diff.txt"
