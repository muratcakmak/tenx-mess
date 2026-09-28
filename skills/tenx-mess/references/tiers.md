# Tiers

Every scan item gets one tier. The tier decides whether the item starts selected at gate one and whether it can enter a plan.

| Tier | Starts selected | Can enter a plan | Meaning |
| --- | --- | --- | --- |
| `finished` | yes | yes | The work shipped (PR merged, or the commits are in the default branch). |
| `rebuildable` | yes | yes | A tool recreates it on the next build or install. |
| `review` | no | yes | Probably safe, but only the user knows. |
| `keep` | no | no | Locked, in use, recent, or state that must stay. |
| `report` | no | no | Shown for information only. |

## Worktree rules

The scan checks these in order. The first match wins.

1. Locked worktree → `keep`.
2. A process has its working directory inside the worktree (a shell, `claude`, Metro, Xcode) → `keep`.
3. `git status` failed → `keep`.
4. Last activity is newer than `stale_days` (default 14) → `keep`. Activity is the newest of: last commit, last HEAD move, last edit to an uncommitted file. The git index time is not used, because any `git status` rewrites it.
5. Detached HEAD whose commit is on no branch → `review`, `strip_deps` only. Removing the worktree would orphan the commit.
6. PR merged, or HEAD is inside the default branch:
   - Clean → `finished`, default `worktree_remove`.
   - Uncommitted files and a merged PR → `finished`, default `strip_deps`.
   - Uncommitted files and no PR (a branch that never got a commit) → `review`, default `strip_deps`. The uncommitted files are the work.
7. Detached HEAD whose commit is on a branch, clean → `finished`.
8. PR closed without merge → `review`, default `worktree_remove` when clean.
9. Open PR or no PR → `review`, default `strip_deps`.

`worktree_remove` is offered only when the worktree is clean and its commit is on a branch. `strip_deps` is offered only when a dependency folder exists and is not a symlink.

## Why a merged check needs GitHub

Squash and rebase merges create new commits on the default branch. The original branch commits are never ancestors of it, so `git branch --merged` misses them. The scan asks `gh pr list` for the PR state of each worktree branch. Without `gh`, it falls back to the ancestry check, and squash-merged worktrees land in `review`.
