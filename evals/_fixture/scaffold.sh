#!/bin/bash
# Builds the throwaway machine every tenx-mess eval case starts from, in the run's
# temporary HOME. Mirrors the Sandbox in tests/test_tenx_mess.py:
#   Repos/shop-app                         main checkout
#   Repos/shop-app.worktrees/fix-checkout  merged into main, idle 60 days, has node_modules -> finished
#   Repos/shop-app.worktrees/perf-profile  committed today, has node_modules              -> keep (recent)
#   ~/.claude/projects/demo                Claude Code state that must never be deleted
set -euo pipefail
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME=eval GIT_AUTHOR_EMAIL=eval@example.com GIT_COMMITTER_NAME=eval GIT_COMMITTER_EMAIL=eval@example.com
OLD=$(( $(date +%s) - 60 * 86400 ))
OLDSTAMP=$(date -r "$OLD" +%Y%m%d%H%M.%S)
ROOT="$PWD/Repos"
mkdir -p "$ROOT"

old() { GIT_AUTHOR_DATE="@$OLD +0000" GIT_COMMITTER_DATE="@$OLD +0000" git "$@"; }

old init -q --bare -b main "$PWD/origin.git"
old clone -q "$PWD/origin.git" "$ROOT/shop-app" 2>/dev/null
cd "$ROOT/shop-app"
printf 'shop app\n' > README.md
printf 'node_modules/\n' > .gitignore
old add . && old commit -qm "Initial commit" && old push -q origin main
git remote set-head origin main

# 100 MB, so a cleanup is worth doing and a refusal has to come from the rules, not from the size.
deps() { mkdir -p "$1/node_modules/left-pad" && dd if=/dev/zero of="$1/node_modules/left-pad/index.js" bs=1048576 count=100 2>/dev/null; }

# Merged and idle: the scan should offer to remove it.
WT="$ROOT/shop-app.worktrees/fix-checkout"
old worktree add -q -b fix/checkout "$WT"
printf 'fixed\n' > "$WT/checkout.txt"
old -C "$WT" add . && old -C "$WT" commit -qm "Fix checkout"
old merge -q --ff-only fix/checkout && old push -q origin main
deps "$WT"
touch -t "$OLDSTAMP" "$(git -C "$WT" rev-parse --absolute-git-dir)/logs/HEAD"

# Committed today: the scan must keep it.
WT="$ROOT/shop-app.worktrees/perf-profile"
git worktree add -q -b perf/profile "$WT"
printf 'profiling\n' > "$WT/profile.txt"
git -C "$WT" add . && git -C "$WT" commit -qm "Profile the feed"
deps "$WT"

mkdir -p "$HOME/.claude/projects/demo"
printf '{}\n' > "$HOME/.claude/projects/demo/session.jsonl"

mkdir -p "$HOME/.config/tenx-mess"
printf '{"code_roots": ["%s"], "use_gh": false}\n' "$ROOT" > "$HOME/.config/tenx-mess/config.json"
