---
name: tenx-mess
description: Finds and cleans the disk mess that agentic coding leaves on a Mac: leftover git worktrees (including Claude Code .claude/worktrees), node_modules and Pods in idle worktrees, Xcode DerivedData, simulators, DeviceSupport, package manager caches (npm, pnpm, bun, Yarn, CocoaPods, Homebrew, Gradle, pip, uv) and orphaned dev servers. Deletes only after two user confirmations. Use when the user says "free up disk space", "clean my mac", "disk is full", "remove old worktrees", "DerivedData is huge", "what is eating my disk", or when a build or install fails with ENOSPC or "No space left on device".
---

# tenx-mess

Your agents ship at 10x speed and leave a 10x mess. This skill finds the mess, sorts it into tiers, and deletes only what the user approves, twice.

The scripts live in `scripts/` next to this file. Run them with `python3`. They need macOS, git, and optionally `gh` for PR state.

## Hard rules

1. Delete only through `scripts/clean.py run`. Never delete, move or `git worktree remove` a scan target with your own Bash commands, even if the user asks you to be quick.
2. Never add `--force` to anything. Never edit files in `~/.tenx-mess/` yourself; only the scripts write there.
3. Items in the `keep` and `report` tiers never go into a plan. If the user wants one gone, explain why it is kept and let them delete it by hand.
4. Never run `clean.py run` in the same turn as `clean.py plan`, except after `plan --approval` succeeds (the user already approved those exact commands on the confirmation page). Never write the approval yourself; only the page writes it.
5. Never delete `~/.claude/projects` or other Claude Code state. Resume and memory need it.

## Flow

### 1. Scan (read-only, no questions)

```bash
python3 <skill-dir>/scripts/scan.py
```

A full scan takes about a minute on a machine with many worktrees. Use `--only worktrees,caches` to limit it, or `--no-gh` when GitHub is not reachable. The script prints a summary grouped by tier and writes the full report to `~/.tenx-mess/reports/`. Read the JSON report when you need details such as dependency sizes or PR numbers.

If the summary says no code roots were found, ask the user where their repos live and write `~/.config/tenx-mess/config.json` (see `config.example.json`).

### 2. Explain

Give the user a short summary before any question:

- free space now and the total the scan found
- the three biggest items, in plain words (for example "3 worktrees whose PRs are merged, 10 GB")
- anything surprising, such as a worktree in `keep` that is huge because it is in use

Keep it to a few lines. The tier table in the scan output already has the detail.

### 3. Choose where the user confirms

- **Confirmation page** (preferred): when the Artifact and ArtifactData tools are available. The user picks items and approves the exact commands on a claude.ai page, which also works on a phone when Claude Code runs on a headless Mac over SSH. Follow "Confirmation page" below, then skip to step 6.
- **Terminal**: when those tools are missing, or the user asks to stay in the terminal. Follow steps 3a and 4.

### Confirmation page

1. Write the preview. It holds every item and the exact commands of each allowed action:

   ```bash
   python3 <skill-dir>/scripts/clean.py preview --out <scratch-dir>/tenx-seed.json
   ```

2. Find the page: `python3 <skill-dir>/scripts/clean.py page-url`. If it prints no URL, publish `<skill-dir>/assets/confirm.html` with the Artifact tool, with `icon: "disk"` and `capabilities: {"db": {"rules": [{"path": "", "read": "view", "write": "owner"}]}}`, then save the URL with `clean.py page-url --set <url>`. Reuse the saved page on every later run, so the user keeps one link.
3. With ArtifactData `batch` on that URL: `set` collection `tenx`, doc `seed` from the preview file (`file_path`), and `delete` the docs `tenx/approval` and `tenx/result`.
4. The first time, tell the user that the page is private to them and holds their repo paths and branch names. Then give the link and end your turn with:

   > Open the page, pick what to clean, review the plan and type `clean` to approve it. Then reply `done` here.

5. When the user replies `done`: read `tenx/approval` with ArtifactData `get` and `out_dir`, then run

   ```bash
   python3 <skill-dir>/scripts/clean.py plan --approval <out_dir>/tenx/approval.json
   ```

   The script refuses an approval from an older preview, one without the typed word `clean`, or one whose commands differ from what it would run. If it refuses, tell the user why and write a new preview. If it succeeds, run `clean.py run --plan <id>` in the same turn.
6. Publish the result to the page: `python3 <skill-dir>/scripts/clean.py result > <scratch-dir>/tenx-result.json`, then ArtifactData `set` `tenx/result` from that file. Continue with step 6 in the chat as well.

### 3a. Gate one in the terminal: pick items

Use AskUserQuestion with `multiSelect: true`, one question per tier that has items, in this order: finished, rebuildable, review. Each question takes 2 to 4 options, so group items:

- Up to 4 items: one option per item.
- More items: group by kind, for example "4 merged worktrees", "Package caches", "Old simulators". Put the total size and the item IDs in each option's description.
- Label format: short name plus size, for example "Merged worktrees · 10 GB".

In the question text, say that finished and rebuildable items are the safe ones and review items need a look. The user can choose "Other" to type item IDs or an action override such as `wt-0b3c39:worktree_remove`.

If the user already named what to clean ("just clear the caches"), skip the question and go straight to gate two with that selection.

### 4. Gate two in the terminal: approve the exact plan

```bash
python3 <skill-dir>/scripts/clean.py plan --select <id>[:action],<id>...
```

`--tiers finished,rebuildable` selects every item in those tiers. The command prints numbered steps with the exact commands and a plan ID. It deletes nothing.

Show the plan output to the user in a code block, unchanged. Then end your turn with:

> Reply `clean` to run plan `<id>`. Any other reply cancels it.

### 5. Run

Only when the user's next message is `clean` (ignore case and surrounding spaces):

```bash
python3 <skill-dir>/scripts/clean.py run --plan <id>
```

Any other reply cancels the plan. If the user asks for changes, make a new plan and go through gate two again.

`clean.py run` checks every step again before it runs it. A worktree that got new changes, moved HEAD, or is now in use by a process is skipped with a reason. Skipped steps are normal. Report them and do not work around them.

### 6. Report

Tell the user:

- free space before and after, from the last line of the run output
- which steps were skipped and why
- the log path, and that a removed worktree comes back with `git -C <repo> worktree add <path> <branch>` (the branch and HEAD are in the log)
- for stripped worktrees: reinstall dependencies (`npm install`, `pnpm install`, `pod install`) before working there again

## Actions

| Action | What it does |
| --- | --- |
| `worktree_remove` | `git worktree remove` without `--force`. Git refuses dirty trees. The branch stays. |
| `strip_deps` | Deletes only dependency and build folders (`node_modules`, `ios/Pods`, `.venv`, ...). Code, commits and uncommitted changes stay. |
| `worktree_prune` | `git worktree prune` for records whose folder is already gone. |
| `tool` | The package manager's own cleaner, such as `pnpm store prune` or `brew cleanup`. |
| `delete_dir` | Deletes a known cache folder or stale DerivedData folders. |
| `simctl_erase` | Erases a shut-down simulator. The device stays. |
| `simctl_delete_unavailable` | Deletes simulators whose runtime is gone. |
| `kill` | Sends SIGTERM to an orphaned dev server. |

For what each detector looks at and why each target is safe, read `references/targets.md`. For the tier rules, read `references/tiers.md`.
