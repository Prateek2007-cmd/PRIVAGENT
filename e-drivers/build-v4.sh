#!/usr/bin/env bash
# build-v4.sh — Build NAVIGATOR-final-v4.zip from the merged tree + staging extras (Stream E).
#
# Usage:
#   ./build-v4.sh <source_dir> <output_zip> [staging_dir] [top_dir_name]
#
#   source_dir   : directory holding the final merged tree (clean checkout
#                  of the FINAL merged commit).
#   output_zip   : destination path, e.g. ~/workspace/your_files/NAVIGATOR-final-v4.zip
#   staging_dir  : directory holding delivery extras NOT in the tracked tree:
#                  start-navigator.bat, start-navigator.ps1, scripts/,
#                  PROVIDER_SETUP.md, FINAL_DELIVERY.md, FINAL_DELIVERY4.md
#                  (default: ~/workspace/navigator-audit/final4/staging).
#                  Staging files are copied to the TOP of the zip (same
#                  layout as the v2 delivery's staging extras).
#   top_dir_name : top-level folder inside the zip (default: NAVIGATOR-final-v4)
#
# Exclusions (never shipped): *.db, node_modules, __pycache__, venvs,
# .git, *.log, secrets (.env* except .env.example, credentials files,
# *.pem, *.key) — same rules as build-v3.sh.
#
# Verification (fails the build if any check fails):
#   1. python zipfile testzip() — every entry's CRC must check out.
#   2. `unzip -l` listing count matches the entries written.
#   3. No excluded pattern appears in the archive's name list.
#   4. All required staging files are present in the archive.
#   5. Prints SHA-256 + size + file count on success.
#
# Exit codes: 0 ok, 1 usage/source error, 2 build error, 3 verification failure.
set -euo pipefail

if [ "$#" -lt 2 ] || [ "$#" -gt 4 ]; then
  echo "usage: $0 <source_dir> <output_zip> [staging_dir] [top_dir_name]" >&2
  exit 1
fi

SRC="$(cd "$1" && pwd)"
OUT="$2"
STAGING="${3:-$HOME/workspace/navigator-audit/final4/staging}"
TOP="${4:-NAVIGATOR-final-v4}"

if [ ! -d "$SRC" ]; then
  echo "error: source dir not found: $SRC" >&2
  exit 1
fi
if [ ! -d "$STAGING" ]; then
  echo "error: staging dir not found: $STAGING" >&2
  exit 1
fi
# Required staging extras (per the v4 delivery task).
for f in start-navigator.bat start-navigator.ps1 PROVIDER_SETUP.md FINAL_DELIVERY.md FINAL_DELIVERY4.md; do
  if [ ! -f "$STAGING/$f" ]; then
    echo "error: required staging file missing: $STAGING/$f" >&2
    exit 1
  fi
done
if [ ! -d "$STAGING/scripts" ]; then
  echo "error: required staging dir missing: $STAGING/scripts" >&2
  exit 1
fi

OUT_ABS="$(mkdir -p "$(dirname "$OUT")" && cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"
if [ "$OUT_ABS" = "$SRC" ] || [[ "$OUT_ABS" == "$SRC/"* ]]; then
  echo "error: output zip must not live inside the source dir" >&2
  exit 1
fi

# --- Build (python3: deterministic, no `zip` binary dependency) ----------------
python3 - "$SRC" "$STAGING" "$OUT_ABS" "$TOP" <<'PYEOF'
import os, sys, zipfile

src, staging, out, top = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

EXCLUDED_DIR_NAMES = {"node_modules", "__pycache__", ".git", "venv", ".venv", "env", ".env-dir"}
EXCLUDED_FILE_SUFFIXES = (".db", ".log", ".pem", ".key")
EXCLUDED_FILE_NAMES = {".env", ".env.local", ".env.production", "credentials.json",
                       "credentials.yaml", "credentials.yml", "credentials.txt",
                       ".netrc", "id_rsa", "id_ed25519"}

def excluded(rel_path: str) -> bool:
    parts = rel_path.replace(os.sep, "/").split("/")
    name = parts[-1]
    if name in EXCLUDED_FILE_NAMES:
        return True
    if name.startswith(".env") and name != ".env.example":
        return True
    if "credentials" in name.lower() and name.lower().endswith((".json", ".yaml", ".yml", ".txt", ".env")):
        return True
    if name.lower().endswith(EXCLUDED_FILE_SUFFIXES):
        return True
    return False

count = 0
skipped = []
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs
                         if d not in EXCLUDED_DIR_NAMES
                         and not d.endswith(".egg-info"))
        rel_root = os.path.relpath(root, src)
        for fname in sorted(files):
            rel = fname if rel_root == "." else os.path.join(rel_root, fname)
            if excluded(rel):
                skipped.append(rel)
                continue
            arc = f"{top}/{rel.replace(os.sep, '/')}"
            zf.write(os.path.join(root, fname), arc)
            count += 1
    # Staging extras at the top level (same layout as the v2 delivery).
    for root, dirs, files in os.walk(staging):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIR_NAMES)
        rel_root = os.path.relpath(root, staging)
        for fname in sorted(files):
            rel = fname if rel_root == "." else os.path.join(rel_root, fname)
            if excluded(rel):
                skipped.append(f"staging/{rel}")
                continue
            arc = f"{top}/{rel.replace(os.sep, '/')}"
            zf.write(os.path.join(root, fname), arc)
            count += 1

print(f"wrote {count} files to {out}")
if skipped:
    print(f"excluded {len(skipped)} entries:")
    for s in skipped:
        print(f"  - {s}")
PYEOF

# --- Verify -------------------------------------------------------------------
echo "verifying $OUT_ABS ..."

python3 - "$OUT_ABS" <<'PYEOF'
import sys, zipfile
path = sys.argv[1]
with zipfile.ZipFile(path) as zf:
    bad = zf.testzip()
    if bad is not None:
        print(f"FAIL: testzip CRC error in entry: {bad}", file=sys.stderr)
        sys.exit(3)
    names = zf.namelist()
print(f"testzip clean: {len(names)} entries")
PYEOF

LISTED="$(unzip -l "$OUT_ABS" | tail -1 | awk '{print $2}')"
WRITTEN="$(python3 -c "import zipfile,sys; print(len(zipfile.ZipFile(sys.argv[1]).namelist()))" "$OUT_ABS")"
if [ "$LISTED" != "$WRITTEN" ]; then
  echo "FAIL: unzip -l count ($LISTED) != zipfile count ($WRITTEN)" >&2
  exit 3
fi
echo "unzip -l count agrees: $LISTED entries"

BAD="$(unzip -l "$OUT_ABS" | grep -Ei '\.db$|node_modules|__pycache__|\.env$|/\.git/|(^|/)venv/|(^|/)\.venv/|\.log$|\.pem$|\.key$' || true)"
if [ -n "$BAD" ]; then
  echo "FAIL: excluded material found inside the zip:" >&2
  echo "$BAD" >&2
  exit 3
fi
echo "no excluded material inside the zip"

# Required staging files must be present at the top level of the zip.
for f in start-navigator.bat start-navigator.ps1 PROVIDER_SETUP.md FINAL_DELIVERY.md FINAL_DELIVERY4.md; do
  if ! unzip -l "$OUT_ABS" | grep -q " $TOP/$f\$"; then
    echo "FAIL: staging file missing from zip: $TOP/$f" >&2
    exit 3
  fi
done
if ! unzip -l "$OUT_ABS" | grep -q " $TOP/scripts/"; then
  echo "FAIL: staging scripts/ missing from zip" >&2
  exit 3
fi
echo "all required staging files present"

sha256sum "$OUT_ABS"
ls -l "$OUT_ABS"
echo "BUILD OK: $OUT_ABS ($WRITTEN files)"
