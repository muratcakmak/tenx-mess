<p align="center">
  <img src="assets/logo.svg" width="128" height="128" alt="tenx-mess logo">
</p>

<h1 align="center">tenx-mess</h1>

<p align="center"><b>Your agents ship at 10x speed and leave a 10x mess.</b><br>
A Claude Code skill that finds what they left on your Mac and deletes it only after you approve twice.</p>

---

Parallel agents are great until the disk says 11 GB free. Every task gets a git worktree. Every worktree gets its own 3 GB `node_modules` and `Pods`. Every build writes DerivedData and every install fills a cache. Nobody cleans any of it, because the agent that made it already finished.

On the machine this was built on, one repo had **62 GB in 16 worktrees**. Almost all of it was `node_modules` for branches whose PRs were merged or closed weeks ago.

A normal Mac cleaner sees folder sizes. tenx-mess reads the context around each folder: the git branch, the PR state on GitHub, uncommitted files, the last real activity, and which processes still use it.

## What it finds

| Detector | Looks at | Offers |
| --- | --- | --- |
| Worktrees | Every linked worktree, including Claude Code `.claude/worktrees` | Remove the worktree, or remove only its dependency folders |
| Dormant checkouts | Repos with no activity for 60 days that still hold `node_modules`, `Pods`, `.venv`, ... | Remove dependency folders |
| Xcode | DerivedData for deleted or idle projects, old DeviceSupport, big or unavailable simulators | Delete, erase |
| Caches | npm, pnpm, Yarn, bun, CocoaPods, Homebrew, Gradle, pip, uv, SwiftPM, Go, Cargo, Playwright, Metro | The tool's own cleaner where one exists |
| Processes | Dev servers still listening on a port after the session that started them ended | SIGTERM |
| Agent state | `~/.claude/projects` and friends | Nothing. It shows the size and never deletes it. |

Each item lands in a tier: **finished** (PR merged), **rebuildable** (a tool recreates it), **review** (your call), **keep** (locked, in use, or recent), or **report**. See [`references/tiers.md`](skills/tenx-mess/references/tiers.md) for the rules.

## How the confirmation works

1. **Scan.** `scan.py` measures everything and writes a JSON report. It changes nothing.
2. **Pick.** You choose what to clean. Finished and rebuildable items start selected. Review items start unselected.
3. **Approve.** You see every exact command and type `clean` to approve them.
4. **Run.** `clean.py run` checks each item again and runs only the approved plan. A worktree that gained changes since the scan is skipped.
5. **Log.** Every run writes a manifest with each path, branch and commit SHA, so any removed worktree can come back.

### On a confirmation page

When Claude Code can publish artifacts, steps 2 and 3 happen on a private claude.ai page. It lists every item with its size and reason, lets you switch between removing a worktree and removing only its dependencies, and shows the plan as a sheet with the exact commands. It also works on a phone, which helps when Claude Code runs on a headless Mac over SSH.

The page never runs anything. It writes your approval to the page's own database, where only you, the owner, can write. Then it sends a comment to Claude, which starts a turn in the Claude Code session watching the page. If no session is watching, you reply `done` in the terminal instead. Claude runs `clean.py plan --approval`, which refuses the approval unless its commands match, character for character, what the script would run. When the run ends, the result shows on the page.

The page holds your repo paths and branch names, and it stays private to you. The template is [`assets/confirm.html`](skills/tenx-mess/assets/confirm.html) and contains no data.

### In the terminal

Without artifacts, Claude asks one multi-select question per tier, prints the plan from `clean.py plan`, and waits for you to reply `clean`. Any other reply cancels.

See [`examples/scan-output.txt`](examples/scan-output.txt) for a full terminal session.

## Safety

- Claude never deletes anything with its own shell commands. All deletion goes through `clean.py`, which builds commands from a fixed table, never from the report.
- Worktrees are removed with `git worktree remove`, never with `--force`. Git refuses dirty trees, and the branch stays.
- A plan is hashed. If the plan file is edited after you saw it, `run` refuses it. Plans expire after two hours.
- Nothing is deleted if a process uses it, if it is a symlink, or if it sits outside the allowed folders. `~`, `~/.claude`, `~/.ssh`, `~/Library`, `~/Documents` and similar are always protected.
- Nothing goes to the Trash. The Trash is on the same disk and frees no space. Rebuildable data is deleted directly, and code goes through git.

## Install

As a Claude Code plugin:

```
/plugin marketplace add muratcakmak/tenx-mess
/plugin install tenx-mess@tenx-mess
```

Or as a plain skill:

```bash
git clone https://github.com/muratcakmak/tenx-mess ~/src/tenx-mess
ln -s ~/src/tenx-mess/skills/tenx-mess ~/.claude/skills/tenx-mess
```

Needs macOS, Python 3 (ships with the Xcode command line tools), and git. `gh` is optional, but without it squash-merged worktrees land in review instead of finished.

## Use

Ask Claude Code something like:

- "My disk is full, clean up after the agents"
- "Remove old worktrees"
- "DerivedData is huge"

Or run the scripts yourself:

```bash
python3 skills/tenx-mess/scripts/scan.py                  # read-only report
python3 skills/tenx-mess/scripts/clean.py plan --tiers finished,rebuildable
python3 skills/tenx-mess/scripts/clean.py run --plan <id>
```

## Configure

tenx-mess looks for repos in `~/Repos`, `~/Developer`, `~/Projects`, `~/code`, `~/src` and a few other common folders. To change that, copy [`config.example.json`](skills/tenx-mess/config.example.json) to `~/.config/tenx-mess/config.json`.

| Key | Default | Meaning |
| --- | --- | --- |
| `code_roots` | common folders | Where to look for repos |
| `repo_depth` | 3 | How deep to look below each root |
| `stale_days` | 14 | Worktrees active more recently than this are kept |
| `dormant_days` | 60 | Main checkouts idle this long get their dependencies offered |
| `min_size_mb` | 50 | Smaller caches are not listed |
| `use_gh` | true | Ask GitHub for PR state |
| `protected_paths` | `[]` | Extra folders that are never touched |

## Test

```bash
python3 -m unittest discover -s tests -v
```

The tests build throwaway repos under a temporary `HOME` and check the refusals too: a worktree that got dirty after the scan, an edited plan, a symlinked `node_modules`.

## License

MIT
