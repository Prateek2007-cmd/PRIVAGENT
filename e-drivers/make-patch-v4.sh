#!/usr/bin/env bash
# make-patch-v4.sh — Build NAVIGATOR-patch-v4.diff (git diff ab003b9..FINAL)
# and verify it applies cleanly to a fresh ab003b9 checkout and reproduces
# the same file set.
# Usage: ./make-patch-v4.sh <final_commit> <output_diff>
#   final_commit : full/short SHA of the merged FINAL commit
#   output_diff  : e.g. ~/workspace/your_files/NAVIGATOR-patch-v4.diff
# Exit 0 only if: diff non-empty, git apply --check passes on a fresh
# ab003b9 worktree, the applied tree matches the final commit's file set
# (git diff --name-only identical) and `git diff <final>` is empty there.
set -euo pipefail
FINAL="${1:?usage: make-patch-v4.sh <final_commit> <output_diff>}"
OUT="${2:?usage: make-patch-v4.sh <final_commit> <output_diff>}"
REPO="$HOME/workspace/navigator-audit/PRIVAGENT"
CHECK="/tmp/patch-check-v4"
rm -rf "$CHECK"
git -C "$REPO" rev-parse --verify "$FINAL^{commit}" >/dev/null
git -C "$REPO" diff "ab003b9..$FINAL" > "$OUT"
LINES="$(wc -l < "$OUT")"
FILES="$(git -C "$REPO" diff --name-only "ab003b9..$FINAL" | sort)"
echo "diff: $LINES lines, $(echo "$FILES" | wc -l) files"
[ "$LINES" -gt 0 ] || { echo "error: empty diff" >&2; exit 1; }
git -C "$REPO" worktree add --detach "$CHECK" ab003b9 >/dev/null 2>&1
git -C "$CHECK" apply --check "$OUT" && echo "CHECK-OK"
git -C "$CHECK" apply "$OUT" && echo "APPLY-OK"
APPLIED_FILES="$(git -C "$CHECK" diff --name-only | sort)"
[ "$FILES" = "$APPLIED_FILES" ] || { echo "error: file set mismatch" >&2; diff <(echo "$FILES") <(echo "$APPLIED_FILES") >&2; exit 1; }
echo "FILESET-IDENTICAL"
# The applied tree must equal the final commit tree (modulo untracked files).
git -C "$CHECK" diff "$FINAL" --stat | tail -2
if [ -n "$(git -C "$CHECK" diff "$FINAL" --name-only)" ]; then
  echo "error: applied tree differs from $FINAL" >&2
  git -C "$CHECK" diff "$FINAL" --name-only >&2
  exit 1
fi
echo "TREE-MATCHES-FINAL"
sha256sum "$OUT"
git -C "$REPO" worktree remove --force "$CHECK"
