# Contributing

Thanks for helping. tenx-mess deletes things on people's machines, so the bar for a change is that it can't delete something it shouldn't. Most of this page is about that.

## Before you start

- **New target or detector:** open an issue first with the [new target template](https://github.com/muratcakmak/tenx-mess/issues/new?template=new_target.yml). Say what creates the folder, how it comes back, and how to tell that it is safe to delete.
- **Bug:** open an issue with the [bug template](https://github.com/muratcakmak/tenx-mess/issues/new?template=bug_report.yml). Scan reports contain your repo paths and branch names, so redact them before pasting.
- **Security problem:** don't open an issue. See [SECURITY.md](SECURITY.md).

## Setup

There is nothing to install. The scripts use only the Python standard library.

```bash
git clone https://github.com/muratcakmak/tenx-mess
ln -s "$PWD/tenx-mess/skills/tenx-mess" ~/.claude/skills/tenx-mess   # optional: use your checkout as the skill
```

The code must run on Python 3.9, the version the Xcode command line tools ship as `/usr/bin/python3`. That rules out `match` and `X | Y` type unions.

## Layout

| Path | What it is |
| --- | --- |
| `skills/tenx-mess/SKILL.md` | What the agent reads: the flow and the hard rules |
| `skills/tenx-mess/scripts/scan.py` | Finds items and sorts them into tiers. Read-only. |
| `skills/tenx-mess/scripts/clean.py` | Plans and runs deletions. The only file that deletes. |
| `skills/tenx-mess/scripts/common.py` | Shared helpers and the fixed tables commands are built from (`CACHES`, `DEP_RELPATHS`) |
| `skills/tenx-mess/references/` | Why each target is safe (`targets.md`) and the tier rules (`tiers.md`) |
| `skills/tenx-mess/assets/confirm.html` | The confirmation page |
| `tests/` | End-to-end tests against throwaway repos |
| `.claude-plugin/` | Plugin and marketplace manifests |

## Safety rules

A change that breaks one of these won't be merged.

- `scan.py` never deletes or moves anything. It writes only its report to `~/.tenx-mess/`. Its `git status` calls can refresh a repo's index, as any `git status` does.
- Program names and flags come only from `clean.py` and the tables in `common.py`, never from the report or the page. Paths, PIDs and simulator IDs from the report are only arguments, and `clean.py run` checks each one again before using it. An edited report must not be able to change what runs.
- No `--force` on git or on anything that can remove code. Git refusing a dirty worktree is a feature. The one `--force` is `npm cache clean --force`, because npm refuses to clean its cache without it.
- `clean.py run` checks every step again right before running it, and skips it if anything changed since the plan.
- Protected paths (`ALWAYS_PROTECTED` and the user's `protected_paths`) are never deleted, and symlinks are never followed.
- Anything in the `keep` or `report` tier never enters a plan.

Every new deletion path needs a test showing that it refuses the unsafe case. Examples include a dirty worktree, a symlink, a path outside the allowed folders, or a process using it.

## Adding a cache

1. Add an entry to `CACHES` in `common.py`. Use the tool's own cleaner in `cmd` when it has one. List in `busy` the processes that must not be running.
2. Explain why it is safe in `references/targets.md`.
3. Add it to the "What it finds" table in the README.

## Tests

See [Test](README.md#test) in the README. Run the suite with both interpreters before opening a PR:

```bash
/usr/bin/python3 -m unittest discover -s tests -v
python3 -m unittest discover -s tests -v
```

CI runs both on macOS for every PR.

## Measuring speed

Point the state folder at a temporary directory, so a benchmark never replaces your real reports or a pending approval:

```bash
TENX_MESS_HOME=$(mktemp -d) python3 skills/tenx-mess/scripts/scan.py --only worktrees,dormant --no-gh
```

Compare the `seconds` field of the two reports. Run the new version first, so a warm file cache favors the old one. Don't benchmark while a cleanup is running on the same disk.

## Writing

Docs and messages use short, plain sentences, like the README. The `description` in `SKILL.md` starts with "Use when" and lists triggers. It never describes the workflow.

## Commits and pull requests

- Commit subjects are imperative and sentence case, without a prefix. For example: "Write the clean log after every step".
- Keep one change per commit, and put the reason in the commit body.
- In the PR description, give one line per change saying what it is for. Note anything removed on purpose, so a reviewer doesn't read it as a regression.
- CI must pass.

## Releasing

For maintainers:

1. Bump `version` in `.claude-plugin/plugin.json`.
2. Add a section to `CHANGELOG.md`.
3. Merge to `main`.
4. Run `gh release create vX.Y.Z --target main --title "tenx-mess X.Y.Z"` with the changelog section as the notes.
