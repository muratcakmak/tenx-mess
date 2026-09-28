#!/usr/bin/env python3
"""tenx-mess clean: turn a selection into a plan, then run exactly that plan.

Two steps, so what runs is what you approved:

  clean.py plan --select wt-e0b479,cache-npm,wt-0b3c39:strip_deps
      Reads the latest scan report, builds every command, prints the plan
      and a plan ID. Deletes nothing.

  clean.py run --plan <id>
      Loads that plan, checks every item again, and runs only the steps
      that still pass. Writes a log to ~/.tenx-mess/log/.

Selection: item IDs from scan.py, each with an optional ":action".
  --tiers finished,rebuildable   selects every item in those tiers
Commands come from this file, never from the report.

Confirmation page (an artifact the user approves on any device):

  clean.py preview --out seed.json
      Writes every item with the exact commands of each allowed action.
      The page shows these and records the user's approval.

  clean.py plan --approval approval.json
      Builds the plan from the approved selection and refuses unless its
      commands match the approved commands exactly.

  clean.py result
      Prints the last run's outcome as JSON for the page.
"""

import argparse
import datetime
import glob
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as c  # noqa: E402

PLAN_TTL_MINUTES = 120


class Refused(Exception):
    """A safety check failed. The step is skipped and the reason is logged."""


def tilde(p):
    s, home = str(p), str(c.HOME)
    return "~" + s[len(home):] if s.startswith(home) else s


def q(p):
    s = tilde(p)
    return "'%s'" % s if " " in s else s


# ---------- guards ----------

def guard_path(path, cfg, allowed_roots):
    """The path must exist, must not be a symlink, must sit inside an allowed root,
    and must not be (or contain) a protected path."""
    p = str(path)
    if not os.path.lexists(p):
        raise Refused("%s no longer exists" % tilde(p))
    if os.path.islink(p):
        raise Refused("%s is a symlink" % tilde(p))
    real = os.path.realpath(p)
    if not any(c.is_under(real, r) and os.path.realpath(real) != os.path.realpath(r) for r in allowed_roots):
        raise Refused("%s is outside the allowed folders" % tilde(p))
    for prot in c.protected_paths(cfg):
        if real == prot or c.is_under(prot, real):
            raise Refused("%s is protected" % tilde(p))
    return real


def guard_no_users(path):
    users = c.users_of(path, c.process_cwds())
    if users:
        raise Refused("in use by %s (pid %s)" % (users[0]["command"], users[0]["pid"]))


def guard_not_busy(names):
    hits = c.busy(names)
    if hits:
        raise Refused("%s is running; quit it and try again" % ", ".join(hits))


def find_worktree(repo, path):
    for e in c.worktrees(repo):
        if os.path.realpath(e["path"]) == os.path.realpath(path):
            return e
    return None


def cache_entry(key):
    for e in c.CACHES:
        if e["key"] == key:
            return e
    return None


# ---------- step building (plan time) ----------

def build_step(item, action, cfg):
    """Return a step dict with the exact command text. Raises Refused for bad selections."""
    if action not in item["actions"]:
        raise Refused("%s does not allow %s (allowed: %s)" % (
            item["id"], action, ", ".join(item["actions"]) or "none"))
    m = item["meta"]
    step = {"id": item["id"], "kind": item["kind"], "action": action, "title": item["title"],
            "bytes": item["bytes"], "meta": m, "path": item["path"]}

    if action == "worktree_remove":
        step["commands"] = ["git -C %s worktree remove %s" % (q(m["repo"]), q(item["path"]))]
        step["note"] = "branch %s stays; head %s" % (m.get("branch") or "(detached)", (m.get("head") or "")[:10])
    elif action == "worktree_prune":
        step["commands"] = ["git -C %s worktree prune" % q(m["repo"])]
        step["bytes"] = 0
    elif action == "strip_deps":
        rels = [r for r in m["deps"] if r in c.DEP_RELPATHS]
        step["targets"] = [str(Path(item["path"]) / r) for r in rels]
        step["bytes"] = m["deps_bytes"]
        step["commands"] = ["rm -rf %s" % q(t) for t in step["targets"]]
        step["note"] = "code and commits stay; reinstall dependencies to work here again"
    elif action == "delete_dir":
        targets = m.get("paths") or [item["path"]]
        step["targets"] = targets
        step["commands"] = ["rm -rf %s" % q(t) for t in targets[:5]]
        if len(targets) > 5:
            step["commands"].append("# ... and %d more folders" % (len(targets) - 5))
    elif action == "tool":
        e = cache_entry(m["key"])
        if not e or not e["cmd"]:
            raise Refused("no cleaner command for %s" % m["key"])
        step["commands"] = [" ".join(e["cmd"])]
    elif action == "simctl_erase":
        step["commands"] = ["xcrun simctl erase %s   # %s" % (m["udid"], m["name"])]
    elif action == "simctl_delete_unavailable":
        step["commands"] = ["xcrun simctl delete unavailable"]
    elif action == "kill":
        step["commands"] = ["kill -TERM %s   # %s" % (m["pid"], m["command"][:80])]
    else:
        raise Refused("unknown action %s" % action)
    return step


# ---------- execution (run time) ----------

def rm_tree(path):
    code = subprocess.run(["/bin/rm", "-rf", "--", path]).returncode
    if code != 0 or os.path.lexists(path):
        raise Refused("rm could not delete all of %s" % tilde(path))


def run_cmd(cmd, cwd=None, timeout=1800):
    out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise Refused("`%s` failed: %s" % (" ".join(cmd), (out.stderr or out.stdout).strip()[:300]))
    return out.stdout


def execute(step, cfg):
    a, m = step["action"], step["meta"]
    roots = c.code_roots(cfg)

    if a == "worktree_remove":
        path = guard_path(step["path"], cfg, roots)
        e = find_worktree(m["repo"], path)
        if e is None or e["main"]:
            raise Refused("not a linked worktree of %s any more" % tilde(m["repo"]))
        if e.get("locked"):
            raise Refused("worktree is locked")
        if e.get("head") != m.get("head"):
            raise Refused("HEAD moved since the scan (%s → %s)" % (m.get("head", "")[:8], (e.get("head") or "")[:8]))
        dirty = c.dirty_count(path)
        if dirty != 0:
            raise Refused("worktree now has %s uncommitted file%s" % (dirty, "" if dirty == 1 else "s"))
        guard_no_users(path)
        run_cmd(["git", "-C", m["repo"], "worktree", "remove", path])  # never --force
        return

    if a == "worktree_prune":
        run_cmd(["git", "-C", m["repo"], "worktree", "prune"])
        return

    if a == "strip_deps":
        root = guard_path(step["path"], cfg, roots)
        e = find_worktree(m["repo"], root)
        if e is None:
            raise Refused("%s is not a git checkout any more" % tilde(root))
        guard_no_users(root)
        for t in step["targets"]:
            rel = os.path.relpath(t, step["path"])
            if rel not in c.DEP_RELPATHS:
                raise Refused("%s is not a dependency folder" % tilde(t))
            if not os.path.lexists(t):
                continue
            real = guard_path(t, cfg, [root])
            if os.path.relpath(real, root) != rel:
                raise Refused("%s resolves outside its checkout" % tilde(t))
            rm_tree(real)
        return

    if a == "delete_dir":
        kind = step["kind"]
        if kind == "cache":
            key = m["key"]
            if key == "metro":
                tmp = os.path.realpath(os.environ.get("TMPDIR", "/tmp"))
                allowed = set(glob.glob(os.path.join(tmp, "metro-*")) + glob.glob(os.path.join(tmp, "haste-map-*")))
                guard_not_busy(["*metro", "*react-native start", "*expo start"])
                for t in step["targets"]:
                    if os.path.realpath(t) not in {os.path.realpath(x) for x in allowed}:
                        raise Refused("%s is not a Metro cache folder" % tilde(t))
                    if os.path.lexists(t):
                        rm_tree(os.path.realpath(t))
                return
            e = cache_entry(key)
            if e is None:
                raise Refused("unknown cache %s" % key)
            expected = c.cache_path(e)
            if expected is None or os.path.realpath(expected) != os.path.realpath(step["path"]):
                raise Refused("cache path changed since the scan")
            guard_not_busy(e["busy"])
            real = guard_path(expected, cfg, [c.HOME])
            rm_tree(real)
            return
        if kind == "derived_data":
            parent = os.path.realpath(c.expand(c.DERIVED_DATA))
            guard_not_busy(["xcodebuild"])
        elif kind == "device_support":
            parent = os.path.realpath(step["path"])
            if not (c.is_under(parent, c.expand(c.XCODE_DEV)) and parent.endswith(" DeviceSupport")):
                raise Refused("not an Xcode DeviceSupport folder")
        else:
            raise Refused("delete_dir is not allowed for %s" % kind)
        for t in step["targets"]:
            if not os.path.lexists(t):
                continue
            real = guard_path(t, cfg, [parent])
            if os.path.dirname(real) != parent:
                raise Refused("%s is not a direct child of %s" % (tilde(t), tilde(parent)))
            rm_tree(real)
        return

    if a == "tool":
        e = cache_entry(m["key"])
        guard_not_busy(e["busy"])
        run_cmd(e["cmd"])
        return

    if a == "simctl_erase":
        code, out = c.run(["xcrun", "simctl", "list", "devices", "-j"])
        devs = [d for ds in json.loads(out or "{}").get("devices", {}).values() for d in ds]
        d = next((d for d in devs if d["udid"] == m["udid"]), None)
        if d is None:
            raise Refused("simulator is gone")
        if d.get("state") != "Shutdown":
            raise Refused("simulator is %s; shut it down first" % d.get("state"))
        run_cmd(["xcrun", "simctl", "erase", m["udid"]])
        return

    if a == "simctl_delete_unavailable":
        run_cmd(["xcrun", "simctl", "delete", "unavailable"])
        return

    if a == "kill":
        pid = int(m["pid"])
        if c.command_line(pid) != m["command"]:
            raise Refused("pid %s is now a different process" % pid)
        if c.ppid_of(pid) != 1:
            raise Refused("process has a parent again")
        os.kill(pid, signal.SIGTERM)
        return

    raise Refused("unknown action %s" % a)


# ---------- commands ----------

def load_report(path):
    p = c.latest_report() if path in (None, "latest") else Path(path)
    return json.loads(Path(p).read_text()), p


def build_steps(report, rpath, picks, cfg):
    """Turn (item id, action or None) picks into steps. Returns (steps, problems)."""
    items = {i["id"]: i for i in report["items"]}
    steps, problems, seen = [], [], set()
    for iid, action in picks:
        if iid in seen:
            continue
        seen.add(iid)
        item = items.get(iid)
        if item is None:
            problems.append("%s: not in report %s" % (iid, tilde(rpath)))
            continue
        if item["tier"] in ("keep", "report"):
            problems.append("%s: tier %s never enters a plan" % (iid, item["tier"]))
            continue
        try:
            steps.append(build_step(item, action or item["default_action"], cfg))
        except Refused as exc:
            problems.append("%s: %s" % (iid, exc))
    return steps, problems


def flat_commands(steps):
    return [cmd for s in steps for cmd in s["commands"]]


def check_approval(approval, rpath):
    """The approval must come from the current preview and carry the typed confirmation."""
    seed_path = c.STATE_DIR / "seed.json"
    if not seed_path.exists():
        raise Refused("no preview found; run clean.py preview first")
    seed = json.loads(seed_path.read_text())
    if approval.get("nonce") != seed["nonce"]:
        raise Refused("approval is for preview %s, the current preview is %s"
                      % (approval.get("nonce"), seed["nonce"]))
    if os.path.realpath(seed["report"]) != os.path.realpath(str(rpath)):
        raise Refused("the preview was made from a different scan report")
    if str(approval.get("typed", "")).strip().lower() != "clean":
        raise Refused("approval does not carry the typed word clean")
    return [(x["id"], x.get("action")) for x in approval.get("selection", [])]


def cmd_plan(args, cfg):
    report, rpath = load_report(args.report)
    picks = []
    approval = None
    if args.approval:
        approval = json.loads(Path(args.approval).read_text())
        try:
            picks = check_approval(approval, rpath)
        except Refused as exc:
            print("tenx-mess: approval refused: %s" % exc)
            return 1
    if args.tiers:
        tiers = set(args.tiers.split(","))
        picks += [(i["id"], None) for i in report["items"]
                  if i["tier"] in tiers and i["default_action"] and i["tier"] not in ("keep", "report")]
    for token in filter(None, (args.select or "").split(",")):
        iid, _, action = token.strip().partition(":")
        picks.append((iid, action or None))

    steps, problems = build_steps(report, rpath, picks, cfg)
    if not steps:
        print("tenx-mess: nothing to plan.")
        for p in problems:
            print("  skipped " + p)
        return 1
    if approval is not None:
        if problems:
            print("tenx-mess: approval refused: some approved items cannot be planned:")
            for p in problems:
                print("  " + p)
            return 1
        if flat_commands(steps) != list(approval.get("commands", [])):
            print("tenx-mess: approval refused: the commands differ from the ones you approved.")
            print("Make a new preview and approve again.")
            return 1

    body = json.dumps(steps, sort_keys=True)
    plan_id = hashlib.sha256(body.encode()).hexdigest()[:8]
    plan = {"id": plan_id, "created": time.time(), "report": str(rpath), "steps": steps,
            "approved_on_page": approval is not None}
    c.write_json(c.PLAN_DIR / ("%s.json" % plan_id), plan)

    total = sum(s["bytes"] for s in steps)
    status = "approved on the page" if approval is not None else "nothing has run yet"
    print("tenx-mess plan %s · %d steps · up to %s · %s" % (plan_id, len(steps), c.human(total), status))
    print("")
    for n, s in enumerate(steps, 1):
        print("%2d. %-16s %9s  %s" % (n, s["id"], c.human(s["bytes"]), s["title"]))
        for cmd in s["commands"]:
            print("      $ " + cmd)
        if s.get("note"):
            print("      # " + s["note"])
    for p in problems:
        print("   skipped " + p)
    print("")
    print("Every step is checked again right before it runs. To run: clean.py run --plan %s" % plan_id)
    return 0


def cmd_preview(args, cfg):
    """Every item with the exact commands of each allowed action, for the confirmation page."""
    report, rpath = load_report(args.report)
    nonce = hashlib.sha256(("%s|%s" % (rpath, time.time())).encode()).hexdigest()[:10]
    items = []
    for item in report["items"]:
        options = {}
        if item["tier"] not in ("keep", "report"):
            for action in item["actions"]:
                try:
                    st = build_step(item, action, cfg)
                except Refused:
                    continue
                options[action] = {"commands": st["commands"], "note": st.get("note", ""),
                                   "bytes": st["bytes"]}
        m = item.get("meta", {})
        items.append({
            "id": item["id"], "kind": item["kind"], "tier": item["tier"], "title": item["title"],
            "reason": item["reason"], "bytes": item["bytes"],
            "default_action": item["default_action"] if item["default_action"] in options else None,
            "options": options,
            "branch": m.get("branch"), "agent": bool(m.get("agent")),
        })
    seed = {"nonce": nonce, "created": report["created"], "report": str(rpath),
            "disk": report["disk"], "notes": report.get("notes", []), "items": items}
    c.write_json(c.STATE_DIR / "seed.json", seed)
    if args.out:
        c.write_json(Path(args.out), seed)
        print("tenx-mess preview %s · %d items · written to %s" % (nonce, len(items), args.out))
    else:
        print(json.dumps(seed))
    return 0


def cmd_result(args, cfg):
    """The last run's outcome as JSON, for the confirmation page."""
    logs = sorted(c.LOG_DIR.glob("clean-*.json"))
    if not logs:
        print("tenx-mess: no run log yet.")
        return 1
    log = json.loads(logs[-1].read_text())
    seed_path = c.STATE_DIR / "seed.json"
    nonce = json.loads(seed_path.read_text())["nonce"] if seed_path.exists() else None
    out = {"nonce": nonce, "plan": log["plan"], "finished": log["finished"],
           "free_before": log["free_before"], "free_after": log["free_after"],
           "log": tilde(logs[-1]),
           "results": [{"id": r["id"], "title": r["title"], "action": r["action"],
                        "status": r["status"], "reason": r["reason"]} for r in log["results"]]}
    print(json.dumps(out))
    return 0


def cmd_page_url(args, cfg):
    """Remember the confirmation page's URL so every run reuses one link."""
    path = c.STATE_DIR / "page.json"
    if args.set:
        c.write_json(path, {"url": args.set})
    if path.exists():
        print(json.loads(path.read_text())["url"])
        return 0
    print("tenx-mess: no confirmation page yet.")
    return 1


def cmd_run(args, cfg):
    path = c.PLAN_DIR / ("%s.json" % args.plan)
    if not path.exists():
        print("tenx-mess: no plan %s. Make one with clean.py plan." % args.plan)
        return 1
    plan = json.loads(path.read_text())
    body = json.dumps(plan["steps"], sort_keys=True)
    if hashlib.sha256(body.encode()).hexdigest()[:8] != plan["id"]:
        print("tenx-mess: plan %s was edited after it was made. Make a new plan." % args.plan)
        return 1
    age_min = (time.time() - plan["created"]) / 60
    if age_min > PLAN_TTL_MINUTES:
        print("tenx-mess: plan %s is %d minutes old. Scan again and make a new plan." % (args.plan, age_min))
        return 1

    free_before = c.disk_free()
    results = []
    for n, s in enumerate(plan["steps"], 1):
        t0 = time.time()
        try:
            execute(s, cfg)
            status, why = "done", ""
        except Refused as exc:
            status, why = "skipped", str(exc)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
            status, why = "failed", "%s: %s" % (type(exc).__name__, exc)
        results.append({"step": n, "id": s["id"], "action": s["action"], "title": s["title"],
                        "path": s["path"], "bytes_expected": s["bytes"], "status": status,
                        "reason": why, "commands": s["commands"], "seconds": round(time.time() - t0, 1),
                        "branch": s["meta"].get("branch"), "head": s["meta"].get("head"),
                        "repo": s["meta"].get("repo")})
        mark = {"done": "✓", "skipped": "–", "failed": "✗"}[status]
        print("%s %2d. %-16s %s%s" % (mark, n, s["id"], s["title"], ("  (" + why + ")") if why else ""))

    free_after = c.disk_free()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log = {"plan": plan["id"], "finished": stamp, "free_before": free_before, "free_after": free_after,
           "results": results}
    log_path = c.LOG_DIR / ("clean-%s-%s.json" % (stamp, plan["id"]))
    c.write_json(log_path, log)
    path.unlink()

    done = len([r for r in results if r["status"] == "done"])
    print("")
    print("%d of %d steps done · free space %s → %s (+%s) · log %s" % (
        done, len(results), c.human(free_before), c.human(free_after),
        c.human(max(0, free_after - free_before)), tilde(log_path)))
    removed = [r for r in results if r["status"] == "done" and r["action"] == "worktree_remove"]
    if removed:
        print("To bring a removed worktree back: git -C <repo> worktree add <path> <branch or head> (both are in the log).")
    return 0 if all(r["status"] != "failed" for r in results) else 2


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="build a plan from a selection; deletes nothing")
    p.add_argument("--report", default="latest", help="scan report path (default: latest)")
    p.add_argument("--select", help="comma-separated item IDs, each with an optional :action")
    p.add_argument("--tiers", help="select every item in these tiers, e.g. finished,rebuildable")
    p.add_argument("--approval", help="approval JSON from the confirmation page")
    r = sub.add_parser("run", help="run a plan made by `plan`")
    r.add_argument("--plan", required=True, help="plan ID printed by `plan`")
    v = sub.add_parser("preview", help="write items and exact commands for the confirmation page")
    v.add_argument("--report", default="latest", help="scan report path (default: latest)")
    v.add_argument("--out", help="write the preview to this file instead of stdout")
    sub.add_parser("result", help="print the last run's outcome as JSON")
    u = sub.add_parser("page-url", help="print or remember the confirmation page URL")
    u.add_argument("--set", help="the artifact URL to remember")
    args = ap.parse_args()
    cfg = c.load_config()
    handlers = {"plan": cmd_plan, "run": cmd_run, "preview": cmd_preview,
                "result": cmd_result, "page-url": cmd_page_url}
    sys.exit(handlers[args.cmd](args, cfg))


if __name__ == "__main__":
    main()
