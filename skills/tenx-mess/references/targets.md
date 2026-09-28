# Targets

What each detector looks at, the action it offers, and how to get the data back.

## Worktrees (`worktrees`)

- Finds main checkouts (a `.git` folder) under the code roots, `repo_depth` levels down (default 3).
- Lists every linked worktree with `git worktree list --porcelain`, including agent worktrees in `.claude/worktrees/`.
- Per worktree: size, dependency folder sizes, uncommitted files, last activity, lock state, PR state (`gh`), and processes using it (`lsof`).
- Actions: `worktree_remove`, `strip_deps`, `worktree_prune`.
- Undo: `git -C <repo> worktree add <path> <branch>`. For `strip_deps`, run the package manager install again.

## Dormant checkouts (`dormant`)

- Main checkouts with no activity for `dormant_days` (default 60) that still hold dependency folders.
- Action: `strip_deps`. The checkout itself is never removed.

## Xcode (`xcode`)

| Target | Rule | Action | Undo |
| --- | --- | --- | --- |
| `~/Library/Developer/Xcode/DerivedData/*` | The project in `info.plist` `WorkspacePath` is gone, or not built for `stale_days` | `delete_dir` | Build again |
| `~/Library/Developer/Xcode/* DeviceSupport/*` | Not used for 90 days | `delete_dir` | Xcode copies symbols again when a device connects |
| `~/Library/Developer/Xcode/Archives` | Report only | none | Needed to symbolicate crash reports. Use Xcode Organizer. |
| Unavailable simulators | Runtime no longer installed | `simctl_delete_unavailable` | Create a new simulator |
| Simulators over 1 GB | Shut down | `simctl_erase` | Apps and data are gone. The device stays. |

## Caches (`caches`)

Every command comes from the `CACHES` table in `scripts/common.py`, never from the report.

| Cache | Path | Action |
| --- | --- | --- |
| npm | `~/.npm/_cacache` | `npm cache clean --force` |
| pnpm | `pnpm store path` | `pnpm store prune` (unused packages only) |
| Yarn | `~/Library/Caches/Yarn` | `yarn cache clean` |
| bun | `~/.bun/install/cache` | `bun pm cache rm` |
| CocoaPods | `~/Library/Caches/CocoaPods` | `pod cache clean --all` |
| Homebrew | `brew --cache` | `brew cleanup --prune=all` |
| Gradle | `~/.gradle/caches` | delete (skipped while a Gradle daemon runs) |
| pip | `~/Library/Caches/pip` | delete |
| uv | `~/.cache/uv` | `uv cache prune` |
| SwiftPM | `~/Library/Caches/org.swift.swiftpm` | delete |
| Xcode | `~/Library/Caches/com.apple.dt.Xcode` | delete (skipped while Xcode runs) |
| Go | `~/Library/Caches/go-build` | `go clean -cache` |
| Cargo | `~/.cargo/registry/cache` | delete |
| Playwright | `~/Library/Caches/ms-playwright` | delete, `review` tier |
| Metro | `$TMPDIR/metro-*`, `$TMPDIR/haste-map-*` | delete |

When the tool is not installed, the folder is deleted instead.

## Processes (`processes`)

- TCP listeners whose process is a dev server (`node`, `bun`, `deno`, `workerd`, `python`, `ruby`, `vite`, ...).
- A listener whose parent is `launchd` (pid 1) lost the session that started it. It goes to `review` with the `kill` action.
- Other listeners are `report` only.

## Agent state (`agents`)

- `~/.claude/projects`, `file-history`, `shell-snapshots`, `todos`, `debug`.
- Always `keep`. The scan shows the size so the cost is visible.

## Protected paths

`clean.py` refuses to delete these, or any folder that contains them: `~`, `~/.claude`, `~/.claude/projects`, `~/.ssh`, `~/.gnupg`, `~/Library`, `~/Library/Keychains`, `~/Documents`, `~/Desktop`, `~/Downloads`, `~/Pictures`, `~/Movies`, `~/Music`, plus `protected_paths` from the config.

## Why not the Trash

The Trash is on the same volume, so moving files there frees no space until it is emptied. Rebuildable data is deleted directly. Worktree code goes through `git worktree remove`, which refuses dirty trees and keeps the branch.
