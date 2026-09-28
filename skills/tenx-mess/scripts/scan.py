#!/usr/bin/env python3
"""tenx-mess scan: find what coding agents and dev tools left on this Mac.

Read-only. Writes one JSON report to ~/.tenx-mess/reports/ and prints a
summary grouped by tier. Nothing is deleted here.

Usage:
  scan.py                    full scan, print summary
  scan.py --only worktrees   limit to some detectors (comma-separated)
  scan.py --json             print the full report as JSON
  scan.py --no-gh            skip GitHub PR lookups
"""

import argparse
import datetime
import json
import os
import platform
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as c  # noqa: E402

DETECTORS = ["worktrees", "dormant", "xcode", "caches", "processes", "agents"]
TIERS = [
    ("finished", "FINISHED", "work that shipped or was abandoned; selected by default"),
    ("rebuildable", "REBUILDABLE", "tools recreate these on the next build or install; selected by default"),
    ("review", "REVIEW", "your call; starts unselected"),
    ("keep", "KEEP", "never enters a plan"),
    ("report", "REPORT ONLY", "shown for information"),
]


def tilde(p):
    s = str(p)
    home = str(c.HOME)
    return "~" + s[len(home):] if s.startswith(home) else s


# ---------- repo discovery ----------

SKIP_DIRS = {"node_modules", "Pods", "build", "DerivedData", "Library", ".build", "target", "vendor"}


def find_repos(roots, depth):
    """Main git checkouts (a .git folder) under the code roots, up to `depth` levels down."""
    repos = []
    for root in roots:
        base = len(root.parts)
        for dirpath, dirnames, _ in os.walk(root):
            here = Path(dirpath)
            if (here / ".git").is_dir():
                repos.append(here)
            if len(here.parts) - base >= depth:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS]
    return sorted(set(repos))


def dep_dirs(root):
    out = []
    for rel in c.DEP_RELPATHS:
        p = Path(root) / rel
        if p.is_dir() and not p.is_symlink():
            out.append(rel)
    return out


def last_activity(path, dirty=None):
    """Newest of: last commit, last HEAD move (checkout, reset), last edit to an uncommitted file.

    The git index is not used: any `git status`, from an IDE or from this scan, rewrites it.
    """
    ts = c.last_commit_ts(path) or 0
    code, gitdir = c.git(path, "rev-parse", "--absolute-git-dir")
    if code == 0:
        reflog = Path(gitdir) / "logs" / "HEAD"
        if reflog.exists():
            ts = max(ts, int(reflog.stat().st_mtime))
    for f in (dirty or [])[:500]:
        try:
            ts = max(ts, int((Path(path) / f).stat().st_mtime))
        except OSError:
            pass
    return ts or None


# ---------- worktrees ----------

def classify_worktree(w, stale_days):
    """Return (tier, reason, actions, default_action). Pure function for tests.

    w keys: locked, users, age_days, merged, pr, detached, on_branch, dirty, deps_bytes.
    """
    actions = []
    if w["deps_bytes"] > 0:
        actions.append("strip_deps")
    safe_to_remove = w["dirty"] == 0 and not (w["detached"] and not w["on_branch"])
    if safe_to_remove:
        actions.append("worktree_remove")

    if w["locked"]:
        return "keep", "locked worktree", [], None
    if w["users"]:
        u = w["users"][0]
        return "keep", "in use by %s (pid %s)" % (u["command"], u["pid"]), [], None
    if w["dirty"] is None:
        return "keep", "git status failed", [], None
    if w["age_days"] is not None and w["age_days"] < stale_days:
        return "keep", "changed %d days ago" % w["age_days"], [], None

    dirty_note = ("%d uncommitted file%s" % (w["dirty"], "" if w["dirty"] == 1 else "s")) if w["dirty"] else "no local changes"
    age = "idle %s days" % w["age_days"]
    pr = w["pr"]
    pr_note = "PR #%s %s" % (pr["number"], pr["state"].lower()) if pr else "no PR"

    if w["detached"] and not w["on_branch"]:
        reason = "detached commit exists only in this worktree, %s" % dirty_note
        return ("review" if actions else "keep"), reason, actions, ("strip_deps" if actions else None)

    if w["merged"]:
        by_pr = bool(pr and pr["state"] == "MERGED")
        what = pr_note if by_pr else "no commits beyond %s" % (w.get("ref") or "the default branch")
        if not by_pr and w["dirty"]:
            # Branched off and never committed: the uncommitted files are the work.
            return "review", "%s, %s, %s" % (what, dirty_note, age), actions, "strip_deps" if "strip_deps" in actions else None
        if safe_to_remove:
            return "finished", "%s, %s" % (what, dirty_note), actions, "worktree_remove"
        if actions:
            return "finished", "%s, %s, so only dependencies go" % (what, dirty_note), actions, "strip_deps"
        return "keep", "%s, %s, nothing safe to remove" % (what, dirty_note), [], None

    if w["detached"] and w["on_branch"] and safe_to_remove:
        return "finished", "detached commit is already on a branch, %s, %s" % (dirty_note, age), actions, "worktree_remove"

    if not actions:
        return "keep", "%s, %s, nothing safe to remove" % (pr_note, dirty_note), [], None
    if pr and pr["state"] == "CLOSED":
        default = "worktree_remove" if safe_to_remove else "strip_deps"
        return "review", "%s without merge, %s, %s" % (pr_note, dirty_note, age), actions, default
    default = "strip_deps" if "strip_deps" in actions else "worktree_remove"
    return "review", "%s, %s, %s" % (pr_note, dirty_note, age), actions, default


def scan_worktrees(cfg, repos, procs, use_gh):
    items = []
    rows = []  # (repo, entry)
    for repo in repos:
        for e in c.worktrees(repo):
            if not e["main"]:
                rows.append((repo, e))

    # Stale worktree records whose folder is gone.
    prunable = {}
    for repo, e in rows:
        if e.get("prunable"):
            prunable.setdefault(repo, []).append(e["path"])
    for repo, paths in prunable.items():
        items.append({
            "id": c.item_id("prune", repo), "kind": "worktree_records", "tier": "finished",
            "title": "%s: %d stale worktree records" % (repo.name, len(paths)),
            "path": str(repo), "bytes": 0,
            "reason": "folders are already gone; git still lists them",
            "actions": ["worktree_prune"], "default_action": "worktree_prune",
            "meta": {"repo": str(repo), "missing": paths},
        })

    live = [(r, e) for r, e in rows if not e.get("prunable") and Path(e["path"]).is_dir()]
    sizes = c.du_many([e["path"] for _, e in live])
    dep_paths = []
    for _, e in live:
        for rel in dep_dirs(e["path"]):
            dep_paths.append(str(Path(e["path"]) / rel))
    dep_sizes = c.du_many(dep_paths)
    prs_by_repo = {}
    default_ref = {}

    for repo, e in live:
        path = e["path"]
        if use_gh and repo not in prs_by_repo:
            prs_by_repo[repo] = c.pr_states(
                repo, [x.get("branch") for r, x in live if r == repo and x.get("branch")])
        if repo not in default_ref:
            default_ref[repo] = c.default_remote_ref(repo)
        files = c.dirty_files(path)
        dirty = None if files is None else len(files)
        activity = last_activity(path, files)
        head = e.get("head")
        branch = e.get("branch")
        detached = bool(e.get("detached"))
        prs = prs_by_repo.get(repo) or {}
        pr = prs.get(branch) if branch else None
        ref = default_ref[repo]
        default_branch = ref.split("/", 1)[-1] if ref else None
        # A head already inside the default branch has nothing that would be lost.
        pr_merged = pr is not None and pr["state"] == "MERGED"
        contained = head is not None and branch != default_branch and c.is_ancestor(repo, head, ref)
        deps = {rel: dep_sizes.get(str(Path(path) / rel), 0) for rel in dep_dirs(path)}
        w = {
            "locked": bool(e.get("locked")),
            "users": c.users_of(path, procs),
            "age_days": c.days_since(activity),
            "merged": pr_merged or contained,
            "pr": pr,
            "ref": ref,
            "detached": detached,
            "on_branch": detached and head is not None and c.on_any_branch(repo, head),
            "dirty": dirty,
            "deps_bytes": sum(deps.values()),
        }
        tier, reason, actions, default = classify_worktree(w, cfg["stale_days"])
        agent = "/.claude/worktrees/" in path
        items.append({
            "id": c.item_id("wt", path), "kind": "worktree", "tier": tier,
            "title": "%s%s" % ("[agent] " if agent else "", tilde(path)),
            "path": path, "bytes": sizes.get(path, 0), "reason": reason,
            "actions": actions, "default_action": default,
            "meta": {
                "repo": str(repo), "branch": branch, "head": head, "detached": detached,
                "locked": w["locked"], "dirty": dirty, "age_days": w["age_days"],
                "pr": pr, "merged": w["merged"], "agent": agent, "deps": deps,
                "deps_bytes": w["deps_bytes"],
            },
        })
    return items


def scan_dormant(cfg, repos, procs):
    """Main checkouts with no activity for dormant_days that still hold dependency folders."""
    items = []
    for repo in repos:
        rels = dep_dirs(repo)
        if not rels:
            continue
        activity = last_activity(repo)
        age = c.days_since(activity)
        if age is None or age < cfg["dormant_days"]:
            continue
        users = c.users_of(repo, procs)
        deps = c.du_many([str(repo / r) for r in rels])
        deps = {r: deps[str(repo / r)] for r in rels}
        total = sum(deps.values())
        if total < cfg["min_size_mb"] * 1e6:
            continue
        tier, reason, actions = "review", "no activity for %d days" % age, ["strip_deps"]
        if users:
            tier, reason, actions = "keep", "in use by %s" % users[0]["command"], []
        items.append({
            "id": c.item_id("dormant", repo), "kind": "dormant_repo", "tier": tier,
            "title": "%s (dependencies only)" % tilde(repo), "path": str(repo), "bytes": total,
            "reason": reason, "actions": actions, "default_action": actions[0] if actions else None,
            "meta": {"repo": str(repo), "deps": deps, "deps_bytes": total, "age_days": age},
        })
    return items


# ---------- Xcode ----------

def workspace_path(dd_folder):
    code, out = c.run(["plutil", "-extract", "WorkspacePath", "raw", "-o", "-",
                       str(Path(dd_folder) / "info.plist")])
    return out if code == 0 and out else None


def scan_xcode(cfg):
    items = []
    min_bytes = cfg["min_size_mb"] * 1e6
    dd = c.expand(c.DERIVED_DATA)
    if dd.is_dir():
        folders = [p for p in dd.iterdir() if p.is_dir() and not p.is_symlink()]
        sizes = c.du_many([str(p) for p in folders])
        stale, recent_bytes = [], 0
        for p in folders:
            ws = workspace_path(p)
            age = c.days_since(p.stat().st_mtime)
            orphan = ws is not None and not Path(ws).exists()
            if orphan or (age is not None and age >= cfg["stale_days"]):
                stale.append({"path": str(p), "bytes": sizes[str(p)], "workspace": ws,
                              "why": "project is gone" if orphan else "not built for %d days" % age})
            else:
                recent_bytes += sizes[str(p)]
        total = sum(s["bytes"] for s in stale)
        if stale and total >= min_bytes:
            orphans = len([s for s in stale if s["why"] == "project is gone"])
            items.append({
                "id": c.item_id("dd", dd), "kind": "derived_data", "tier": "rebuildable",
                "title": "Xcode DerivedData: %d stale folders" % len(stale), "path": str(dd),
                "bytes": total,
                "reason": "%d for projects that are gone, %d not built for %d+ days"
                          % (orphans, len(stale) - orphans, cfg["stale_days"]),
                "actions": ["delete_dir"], "default_action": "delete_dir",
                "meta": {"paths": [s["path"] for s in stale], "folders": stale,
                         "recent_bytes": recent_bytes},
            })

    dev = c.expand(c.XCODE_DEV)
    if dev.is_dir():
        for sup in sorted(dev.glob("* DeviceSupport")):
            olds = [p for p in sup.iterdir() if p.is_dir()
                    and (c.days_since(p.stat().st_mtime) or 0) >= 90]
            sizes = c.du_many([str(p) for p in olds])
            total = sum(sizes.values())
            if olds and total >= min_bytes:
                items.append({
                    "id": c.item_id("devsup", sup), "kind": "device_support", "tier": "rebuildable",
                    "title": "%s: %d old OS versions" % (sup.name, len(olds)), "path": str(sup),
                    "bytes": total,
                    "reason": "not used for 90+ days; Xcode copies symbols again when a device connects",
                    "actions": ["delete_dir"], "default_action": "delete_dir",
                    "meta": {"paths": [str(p) for p in olds]},
                })
        arch = dev / "Archives"
        if arch.is_dir():
            b = c.du_bytes(arch)
            if b >= min_bytes:
                items.append({
                    "id": c.item_id("arch", arch), "kind": "archives", "tier": "report",
                    "title": "Xcode Archives", "path": str(arch), "bytes": b,
                    "reason": "needed to symbolicate crash reports; delete old ones in Xcode Organizer",
                    "actions": [], "default_action": None, "meta": {},
                })

    if c.have("xcrun"):
        code, out = c.run(["xcrun", "simctl", "list", "devices", "-j"], timeout=60)
        if code == 0 and out:
            try:
                devices = json.loads(out).get("devices", {})
            except ValueError:
                devices = {}
            unavailable, big = [], []
            for runtime, devs in devices.items():
                for d in devs:
                    if not d.get("dataPath"):
                        continue
                    if not d.get("isAvailable", True):
                        unavailable.append(d)
                    else:
                        big.append((runtime, d))
            if unavailable:
                sizes = c.du_many([d["dataPath"] for d in unavailable])
                items.append({
                    "id": c.item_id("simun", "unavailable"), "kind": "simulators_unavailable",
                    "tier": "rebuildable", "title": "%d unavailable simulators" % len(unavailable),
                    "path": "", "bytes": sum(sizes.values()),
                    "reason": "their iOS runtime is no longer installed",
                    "actions": ["simctl_delete_unavailable"],
                    "default_action": "simctl_delete_unavailable",
                    "meta": {"udids": [d["udid"] for d in unavailable]},
                })
            sizes = c.du_many([d["dataPath"] for _, d in big])
            for runtime, d in big:
                b = sizes[d["dataPath"]]
                if b < 1e9:
                    continue
                booted = d.get("state") == "Booted"
                items.append({
                    "id": c.item_id("sim", d["udid"]), "kind": "simulator",
                    "tier": "keep" if booted else "review",
                    "title": "Simulator %s (%s)" % (d["name"], runtime.rsplit(".", 1)[-1]),
                    "path": d["dataPath"], "bytes": b,
                    "reason": "booted now" if booted else "erase removes apps and data, keeps the device",
                    "actions": [] if booted else ["simctl_erase"],
                    "default_action": None if booted else "simctl_erase",
                    "meta": {"udid": d["udid"], "name": d["name"], "runtime": runtime},
                })
    return items


# ---------- caches ----------

def scan_caches(cfg):
    items = []
    entries = []
    for e in c.CACHES:
        p = c.cache_path(e)
        if p is not None and p.is_dir():
            entries.append((e, p))
    sizes = c.du_many([str(p) for _, p in entries])
    for e, p in entries:
        b = sizes[str(p)]
        if b < cfg["min_size_mb"] * 1e6:
            continue
        tool_ok = e["cmd"] is not None and c.have(e["cmd"][0])
        action = "tool" if tool_ok else "delete_dir"
        reason = "rebuilt on next install"
        if tool_ok:
            reason += "; cleaned with `%s`" % " ".join(e["cmd"])
        if e.get("partial") and tool_ok:
            reason += " (frees unused entries only, so the real gain can be smaller)"
        if e.get("note"):
            reason += "; " + e["note"]
        items.append({
            "id": "cache-" + e["key"], "kind": "cache", "tier": e.get("tier", "rebuildable"),
            "title": e["name"], "path": str(p), "bytes": b, "reason": reason,
            "actions": [action], "default_action": action,
            "meta": {"key": e["key"]},
        })

    tmp = Path(os.environ.get("TMPDIR", "/tmp"))
    metro = [p for p in tmp.glob("metro-*") if p.is_dir()] + [p for p in tmp.glob("haste-map-*") if p.is_dir()]
    if metro:
        sizes = c.du_many([str(p) for p in metro])
        total = sum(sizes.values())
        if total >= cfg["min_size_mb"] * 1e6:
            items.append({
                "id": "cache-metro", "kind": "cache", "tier": "rebuildable",
                "title": "Metro bundler cache (%d folder%s)" % (len(metro), "" if len(metro) == 1 else "s"), "path": str(tmp),
                "bytes": total, "reason": "React Native rebuilds it on the next bundle",
                "actions": ["delete_dir"], "default_action": "delete_dir",
                "meta": {"key": "metro", "paths": [str(p) for p in metro]},
            })
    return items


# ---------- processes ----------

def scan_processes(procs):
    """Dev servers that listen on a port. One item per process, with all its ports."""
    by_pid = {}
    for l in c.listeners():
        name = os.path.basename(l["command"] or "")
        if name in c.DEV_SERVER_COMMANDS:
            by_pid.setdefault(l["pid"], {"name": name, "ports": []})["ports"].append(l["port"])
    items = []
    for pid, info in sorted(by_pid.items()):
        ppid = c.ppid_of(pid)
        cwd = procs.get(pid, (None, None))[1]
        orphan = ppid == 1
        ports = ", ".join(sorted(set(info["ports"]), key=lambda x: int(x) if x.isdigit() else 0))
        items.append({
            "id": c.item_id("proc", pid), "kind": "process",
            "tier": "review" if orphan else "report",
            "title": "%s on port %s (pid %s)" % (info["name"], ports, pid),
            "path": cwd or "", "bytes": 0,
            "reason": ("parent exited, so nothing will stop it" if orphan
                       else "still attached to a parent process")
                      + (", runs in %s" % tilde(cwd) if cwd else ""),
            "actions": ["kill"] if orphan else [], "default_action": "kill" if orphan else None,
            "meta": {"pid": pid, "ports": info["ports"], "command": c.command_line(pid) or info["name"],
                     "ppid": ppid},
        })
    return items


# ---------- agent state ----------

def scan_agents():
    paths = [c.expand(p) for p in c.CLAUDE_STATE if c.expand(p).exists()]
    if not paths:
        return []
    sizes = c.du_many([str(p) for p in paths])
    return [{
        "id": "agent-claude", "kind": "agent_state", "tier": "keep",
        "title": "Agent session data", "path": str(c.expand("~/.claude")),
        "bytes": sum(sizes.values()),
        "reason": "resume, rewind and memory need it; tenx-mess never deletes it",
        "actions": [], "default_action": None,
        "meta": {"parts": {tilde(k): v for k, v in sizes.items()}},
    }]


# ---------- main ----------

def summarize(report):
    total, free = report["disk"]["total"], report["disk"]["free"]
    lines = ["tenx-mess scan · %s free of %s · report %s" % (
        c.human(free), c.human(total), tilde(report["report_path"]))]
    if report["notes"]:
        lines += ["note: " + n for n in report["notes"]]
    for key, label, desc in TIERS:
        group = [i for i in report["items"] if i["tier"] == key]
        if not group:
            continue
        size = sum(i["bytes"] for i in group)
        lines.append("")
        lines.append("%s · %s · %s" % (label, c.human(size), desc))
        for i in group:
            act = (" → " + i["default_action"]) if i["default_action"] else ""
            if i["kind"] in ("worktree", "dormant_repo") and i["default_action"] == "strip_deps":
                act += " (%s)" % c.human(i["meta"]["deps_bytes"])
            lines.append("  %-16s %9s  %s" % (i["id"], c.human(i["bytes"]), i["title"]))
            lines.append("  %-16s %9s  %s%s" % ("", "", i["reason"], act))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated detectors: " + ",".join(DETECTORS))
    ap.add_argument("--json", action="store_true", help="print the full report as JSON")
    ap.add_argument("--no-gh", action="store_true", help="skip GitHub PR lookups")
    args = ap.parse_args()

    if platform.system() != "Darwin":
        print("tenx-mess: this skill targets macOS; some detectors will find nothing.", file=sys.stderr)

    cfg = c.load_config()
    only = set(args.only.split(",")) if args.only else set(DETECTORS)
    unknown = only - set(DETECTORS)
    if unknown:
        ap.error("unknown detectors: " + ", ".join(sorted(unknown)))

    started = time.time()
    roots = c.code_roots(cfg)
    notes = []
    if not roots and only & {"worktrees", "dormant"}:
        notes.append("no code roots found; set code_roots in %s" % tilde(c.CONFIG_PATH))
    use_gh = cfg["use_gh"] and not args.no_gh
    if use_gh and not c.have("gh"):
        notes.append("gh is not installed, so PR state is unknown; merged checks use git only")
        use_gh = False

    procs = c.process_cwds()
    repos = find_repos(roots, cfg["repo_depth"]) if only & {"worktrees", "dormant"} else []
    items = []
    if "worktrees" in only:
        items += scan_worktrees(cfg, repos, procs, use_gh)
    if "dormant" in only:
        items += scan_dormant(cfg, repos, procs)
    if "xcode" in only:
        items += scan_xcode(cfg)
    if "caches" in only:
        items += scan_caches(cfg)
    if "processes" in only:
        items += scan_processes(procs)
    if "agents" in only:
        items += scan_agents()

    order = {k: n for n, (k, _, _) in enumerate(TIERS)}
    items.sort(key=lambda i: (order[i["tier"]], -i["bytes"]))

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = c.REPORT_DIR / ("scan-%s.json" % stamp)

    du = shutil.disk_usage(str(c.HOME))
    report = {
        "version": 1, "created": stamp, "seconds": round(time.time() - started, 1),
        "report_path": str(path),
        "disk": {"total": du.total, "free": du.free},
        "code_roots": [str(r) for r in roots], "repos": len(repos),
        "detectors": sorted(only), "notes": notes, "items": items,
    }
    c.write_json(path, report)
    print(json.dumps(report, indent=2) if args.json else summarize(report))


if __name__ == "__main__":
    main()
