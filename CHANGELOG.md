# Changelog

All notable changes to tenx-mess. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-10-23

### Changed

- The scan is twice as fast. Checking worktrees and dormant repos went from 133s to 66s on a Mac with 40 worktrees across 138 repos (`--only worktrees,dormant --no-gh`).
  - Each worktree is sized in one `du` walk. Before, its `node_modules` and `Pods` were walked a second time.
  - Worktrees, dormant repos and GitHub PR lookups are checked 8 at a time.
- The skill description leads with its trigger phrases. The phrases themselves are unchanged.

### Added

- `clean.py run` writes its log after every step. A run killed partway still records the branch and HEAD to bring back each removed worktree.
- A contributing guide, a security policy, a code of conduct, issue and PR templates, and CI on macOS.

### Fixed

- A DerivedData folder or worktree deleted during a scan no longer crashes it.
- The confirmation page shows a killed run as "Cleanup stopped early" instead of as finished.

## [0.1.0] - 2026-09-28

### Added

- A read-only scan of worktrees, dormant checkouts, Xcode data, package manager caches and orphaned dev servers. Each item is sorted into a tier: finished, rebuildable, review, keep or report.
- Two confirmations before anything is deleted, in the terminal or on a claude.ai confirmation page.
- Hashed plans that expire after two hours, and checks at run time that skip anything that changed since the plan.

[0.2.0]: https://github.com/muratcakmak/tenx-mess/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/muratcakmak/tenx-mess/releases/tag/v0.1.0
