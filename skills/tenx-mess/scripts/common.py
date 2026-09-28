"""Shared helpers for tenx-mess: config, paths, sizes, git, and process checks.

Everything here is read-only. Deletion lives in clean.py only.
"""

import hashlib
import json
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HOME = Path.home()
STATE_DIR = Path(os.environ.get("TENX_MESS_HOME", HOME / ".tenx-mess"))
REPORT_DIR = STATE_DIR / "reports"
PLAN_DIR = STATE_DIR / "plans"
LOG_DIR = STATE_DIR / "log"
CONFIG_PATH = Path(
    os.environ.get("TENX_MESS_CONFIG", HOME / ".config" / "tenx-mess" / "config.json")
)

DEFAULT_CODE_ROOTS = [
    "~/Repos", "~/repos", "~/Developer", "~/Projects", "~/projects",
    "~/code", "~/Code", "~/src", "~/dev", "~/work", "~/GitHub", "~/git",
]

DEFAULT_CONFIG = {
    "code_roots": DEFAULT_CODE_ROOTS,
    "repo_depth": 3,
    "stale_days": 14,
    "dormant_days": 60,
    "min_size_mb": 50,
    "use_gh": True,
    "protected_paths": [],
}

# Dependency and build folders that a package manager or build tool can recreate.
# Paths are relative to a worktree or repo root.
DEP_RELPATHS = [
    "node_modules",
    "ios/Pods",
    "ios/build",
    "android/build",
    "android/app/build",
    "android/.gradle",
    ".venv",
    "venv",
    "target",
    ".next",
    ".turbo",
    ".expo",
    ".build",
    "DerivedData",
]

# Package manager and tool caches. clean.py builds every command from this table,
# never from the report, so an edited report cannot inject a command.
# "path" is a fixed path or a command whose output is the path.
# "cmd" is the tool's own cleaner; None means tenx-mess deletes the folder.
# "busy" lists process names that must not be running during the clean.
CACHES = [
    {"key": "npm", "name": "npm cache", "path": "~/.npm/_cacache",
     "cmd": ["npm", "cache", "clean", "--force"], "busy": []},
    {"key": "pnpm", "name": "pnpm store", "path_cmd": ["pnpm", "store", "path"],
     "cmd": ["pnpm", "store", "prune"], "busy": ["pnpm"], "partial": True},
    {"key": "yarn", "name": "Yarn cache", "path": "~/Library/Caches/Yarn",
     "cmd": ["yarn", "cache", "clean"], "busy": ["yarn"]},
    {"key": "bun", "name": "bun cache", "path": "~/.bun/install/cache",
     "cmd": ["bun", "pm", "cache", "rm"], "busy": []},
    {"key": "cocoapods", "name": "CocoaPods cache", "path": "~/Library/Caches/CocoaPods",
     "cmd": ["pod", "cache", "clean", "--all"], "busy": ["pod"]},
    {"key": "homebrew", "name": "Homebrew cache", "path_cmd": ["brew", "--cache"],
     "cmd": ["brew", "cleanup", "--prune=all"], "busy": ["brew"], "partial": True},
    {"key": "gradle", "name": "Gradle caches", "path": "~/.gradle/caches",
     "cmd": None, "busy": ["*GradleDaemon", "gradle"]},
    {"key": "pip", "name": "pip cache", "path": "~/Library/Caches/pip", "cmd": None, "busy": ["pip", "pip3"]},
    {"key": "uv", "name": "uv cache", "path": "~/.cache/uv",
     "cmd": ["uv", "cache", "prune"], "busy": ["uv"], "partial": True},
    {"key": "swiftpm", "name": "SwiftPM cache", "path": "~/Library/Caches/org.swift.swiftpm",
     "cmd": None, "busy": ["swift-build", "xcodebuild"]},
    {"key": "xcode-cache", "name": "Xcode cache", "path": "~/Library/Caches/com.apple.dt.Xcode",
     "cmd": None, "busy": ["Xcode", "xcodebuild"]},
    {"key": "go-build", "name": "Go build cache", "path": "~/Library/Caches/go-build",
     "cmd": ["go", "clean", "-cache"], "busy": ["go"]},
    {"key": "cargo", "name": "Cargo download cache", "path": "~/.cargo/registry/cache",
     "cmd": None, "busy": ["cargo"]},
    {"key": "playwright", "name": "Playwright browsers", "path": "~/Library/Caches/ms-playwright",
     "cmd": None, "busy": [], "tier": "review",
     "note": "run `npx playwright install` to get them back"},
]

DERIVED_DATA = "~/Library/Developer/Xcode/DerivedData"
XCODE_DEV = "~/Library/Developer/Xcode"
CLAUDE_STATE = ["~/.claude/projects", "~/.claude/file-history", "~/.claude/shell-snapshots",
                "~/.claude/todos", "~/.claude/debug"]
DEV_SERVER_COMMANDS = {"node", "bun", "deno", "workerd", "python", "python3", "Python",
                       "ruby", "esbuild", "vite", "next-server", "php"}


def cache_path(entry):
    if "path" in entry:
        return expand(entry["path"])
    tool = entry["path_cmd"][0]
    if not have(tool):
        return None
    code, out = run(entry["path_cmd"], timeout=30)
    return expand(out.splitlines()[-1]) if code == 0 and out else None


def busy(names):
    """Names from `names` that are running now.

    A plain name matches the process name. A name that starts with "*" matches
    anywhere in the full command line (the Gradle daemon runs as `java`).
    """
    if not names:
        return []
    _, comms = run(["ps", "-u", str(os.getuid()), "-o", "comm="])
    _, cmds = run(["ps", "-u", str(os.getuid()), "-o", "command="])
    running = {os.path.basename(l.strip()) for l in comms.splitlines()}
    hits = []
    for n in names:
        if n.startswith("*"):
            if n[1:] in cmds:
                hits.append(n[1:])
        elif n in running:
            hits.append(n)
    return hits


# Paths that no action may delete, whatever the report says.
ALWAYS_PROTECTED = [
    "~",
    "~/.claude",
    "~/.claude/projects",
    "~/.ssh",
    "~/.gnupg",
    "~/Library",
    "~/Library/Keychains",
    "~/Documents",
    "~/Desktop",
    "~/Downloads",
    "~/Pictures",
    "~/Movies",
    "~/Music",
]


def expand(p):
    return Path(os.path.expandvars(os.path.expanduser(str(p))))


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text())
            cfg.update({k: v for k, v in user.items() if k in DEFAULT_CONFIG})
        except (OSError, ValueError) as exc:
            raise SystemExit("tenx-mess: cannot read %s: %s" % (CONFIG_PATH, exc))
    cfg["code_roots"] = [str(expand(r)) for r in cfg["code_roots"]]
    return cfg


def code_roots(cfg):
    """Existing code roots, with case-insensitive duplicates removed (APFS)."""
    seen, roots = set(), []
    for r in cfg["code_roots"]:
        p = Path(r)
        if not p.is_dir():
            continue
        real = os.path.realpath(p)
        if real.lower() in seen:
            continue
        seen.add(real.lower())
        roots.append(Path(real))
    return roots


def protected_paths(cfg):
    return {os.path.realpath(expand(p)) for p in ALWAYS_PROTECTED + list(cfg["protected_paths"])}


def is_under(child, parent):
    child, parent = os.path.realpath(child), os.path.realpath(parent)
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def item_id(prefix, key):
    return "%s-%s" % (prefix, hashlib.sha1(str(key).encode()).hexdigest()[:6])


def run(cmd, cwd=None, timeout=60):
    """Run a command and return (code, stdout). Never raises for a failed command."""
    try:
        out = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        return out.returncode, out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""


def have(tool):
    return shutil.which(tool) is not None


def du_bytes(path):
    """Disk usage in bytes. du counts APFS clones and hard links once per call."""
    code, out = run(["du", "-sk", str(path)], timeout=600)
    if code not in (0, 1) or not out:
        return 0
    try:
        return int(out.split()[0]) * 1024
    except ValueError:
        return 0


def du_many(paths, workers=8):
    paths = list(paths)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(zip(paths, pool.map(du_bytes, paths)))


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return ("%.0f %s" if unit in ("B", "KB") else "%.1f %s") % (n, unit)
        n /= 1000.0


def days_since(ts):
    return int((time.time() - ts) / 86400) if ts else None


def disk_free():
    return shutil.disk_usage(str(HOME)).free


# ---------- git ----------

def git(repo, *args, timeout=60):
    return run(["git", "-C", str(repo)] + list(args), timeout=timeout)


def worktrees(repo):
    """Parse `git worktree list --porcelain` into dicts. The first entry is the main worktree."""
    code, out = git(repo, "worktree", "list", "--porcelain")
    if code != 0:
        return []
    entries, cur = [], {}
    for line in out.splitlines() + [""]:
        if not line:
            if cur:
                entries.append(cur)
            cur = {}
            continue
        key, _, val = line.partition(" ")
        if key == "worktree":
            cur["path"] = val
        elif key == "HEAD":
            cur["head"] = val
        elif key == "branch":
            cur["branch"] = val.replace("refs/heads/", "", 1)
        elif key in ("detached", "bare", "locked", "prunable"):
            cur[key] = val or True
    for i, e in enumerate(entries):
        e["main"] = i == 0
    return entries


def dirty_files(path):
    """Paths with uncommitted changes, or None when git status fails."""
    code, out = git(path, "status", "--porcelain", "--untracked-files=normal", timeout=120)
    if code != 0:
        return None
    return [l[3:].split(" -> ")[-1].strip('"') for l in out.splitlines() if l.strip()]


def dirty_count(path):
    files = dirty_files(path)
    return None if files is None else len(files)


def last_commit_ts(path):
    code, out = git(path, "log", "-1", "--format=%ct")
    return int(out) if code == 0 and out.isdigit() else None


def default_remote_ref(repo):
    code, out = git(repo, "rev-parse", "--abbrev-ref", "origin/HEAD")
    if code == 0 and out and out != "origin/HEAD":
        return out
    for ref in ("origin/main", "origin/master"):
        if git(repo, "rev-parse", "--verify", "--quiet", ref)[0] == 0:
            return ref
    return None


def is_ancestor(repo, sha, ref):
    return bool(ref) and git(repo, "merge-base", "--is-ancestor", sha, ref)[0] == 0


def on_any_branch(repo, sha):
    code, out = git(repo, "branch", "-a", "--contains", sha, "--format=%(refname)")
    return code == 0 and bool(out.strip())


PR_RANK = {"OPEN": 3, "MERGED": 2, "CLOSED": 1}


def _best_prs(out, into):
    for pr in json.loads(out):
        head = pr.get("headRefName")
        old = into.get(head)
        if old is None or PR_RANK.get(pr["state"], 0) > PR_RANK.get(old["state"], 0):
            into[head] = {"state": pr["state"], "number": pr["number"]}


def pr_states(repo, branches):
    """Map head branch -> {state, number}. One bulk gh call, then one call per branch it missed."""
    if not have("gh") or not branches:
        return None
    result = {}
    code, out = run(
        ["gh", "pr", "list", "--state", "all", "--limit", "400", "--json", "headRefName,state,number"],
        cwd=str(repo), timeout=90,
    )
    if code != 0:
        return None
    try:
        _best_prs(out or "[]", result)
        for b in branches:
            if b in result:
                continue
            code, out = run(
                ["gh", "pr", "list", "--state", "all", "--head", b, "--json", "headRefName,state,number"],
                cwd=str(repo), timeout=60,
            )
            if code == 0 and out:
                _best_prs(out, result)
    except (ValueError, KeyError):
        return None
    return result


# ---------- processes ----------

def process_cwds():
    """Map pid -> (command, cwd) for this user's processes."""
    code, out = run(["lsof", "-a", "-d", "cwd", "-u", str(os.getuid()), "-Fpcn"], timeout=60)
    procs, pid, cmd = {}, None, None
    for line in out.splitlines():
        tag, val = line[:1], line[1:]
        if tag == "p":
            pid, cmd = int(val), None
        elif tag == "c":
            cmd = val
        elif tag == "n" and pid is not None:
            procs[pid] = (cmd or "?", val)
    procs.pop(os.getpid(), None)
    return procs


def users_of(path, procs):
    return [
        {"pid": pid, "command": cmd}
        for pid, (cmd, cwd) in procs.items()
        if is_under(cwd, path)
    ]


def listeners():
    """TCP listeners of this user: list of {pid, command, port}."""
    code, out = run(["lsof", "-nP", "-a", "-iTCP", "-sTCP:LISTEN", "-u", str(os.getuid()), "-Fpcn"], timeout=60)
    res, pid, cmd, seen = [], None, None, set()
    for line in out.splitlines():
        tag, val = line[:1], line[1:]
        if tag == "p":
            pid = int(val)
        elif tag == "c":
            cmd = val
        elif tag == "n" and pid is not None:
            port = val.rsplit(":", 1)[-1]
            if (pid, port) not in seen:
                seen.add((pid, port))
                res.append({"pid": pid, "command": cmd, "port": port})
    return res


def ppid_of(pid):
    code, out = run(["ps", "-o", "ppid=", "-p", str(pid)])
    return int(out) if code == 0 and out.strip().isdigit() else None


def command_line(pid):
    code, out = run(["ps", "-o", "command=", "-p", str(pid)])
    return out if code == 0 else None


# ---------- reports ----------

def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def latest_report():
    reports = sorted(REPORT_DIR.glob("scan-*.json"))
    if not reports:
        raise SystemExit("tenx-mess: no scan report found. Run scan.py first.")
    return reports[-1]
