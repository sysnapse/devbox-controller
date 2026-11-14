#!/usr/bin/env python3
"""Live end-to-end check against a running controller.

Reads the token from DCA_TOKEN (or DEVBOX_TOKEN) and the base URL from DCA_URL
(falling back to autodetection like the client does).

    python3 tests/e2e_live.py

Every step prints PASS/FAIL plus the raw output that justifies it.
"""
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
DC = ROOT / "client" / "dc"

TOKEN = os.environ.get("DCA_TOKEN") or os.environ.get("DEVBOX_TOKEN") or ""
if not TOKEN:
    sys.exit("no token: set DEVBOX_TOKEN or DCA_TOKEN")

env = dict(os.environ)
env["DCA_TOKEN"] = TOKEN

fails = []


def check(label, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + label + ("  | " + detail if detail else ""))
    if not ok:
        fails.append(label)


def dc(*args, timeout=200):
    p = subprocess.run([sys.executable, str(DC), *args], capture_output=True,
                       text=True, env=env, timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def find_base():
    base = os.environ.get("DCA_URL", "").rstrip("/")
    if base:
        return base
    port = os.environ.get("DCA_PORT", "47123")
    cands = []
    try:
        with open("/proc/net/route", encoding="utf-8") as fh:
            for line in fh.readlines()[1:]:
                f = line.split()
                if len(f) > 2 and f[1] == "00000000":
                    raw = int(f[2], 16)
                    cands.append("http://%d.%d.%d.%d:%s" % (
                        raw & 255, (raw >> 8) & 255, (raw >> 16) & 255,
                        (raw >> 24) & 255, port))
                    break
    except OSError:
        pass
    cands += ["http://172.17.0.1:" + port, "http://127.0.0.1:" + port]
    for cand in cands:
        try:
            urllib.request.urlopen(cand + "/v1/health", timeout=2)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return cand
        except Exception:  # noqa: BLE001
            continue
    return ""


def template_names(table_output):
    names = []
    for line in table_output.splitlines():
        parts = line.split()
        if parts and parts[0] != "[broken":
            names.append(parts[0])
    return names


def main():
    task = "e2e1"
    print("== controller reachable")
    rc, out, err = dc("health")
    check("dc health rc=0", rc == 0, (out or err)[:120])

    base = find_base()
    if base:
        try:
            urllib.request.urlopen(base + "/v1/health", timeout=3)
            check("no token must be 401", False, "request was let through")
        except urllib.error.HTTPError as exc:
            check("no token must be 401", exc.code == 401, "HTTP " + str(exc.code))
    else:
        check("controller base URL found", False, "no candidate answered")

    print("== templates")
    rc, out, err = dc("templates")
    names = template_names(out)
    check("templates not empty", bool(names), ",".join(names) or (out or err)[:100])
    check("node22 present", "node22" in names)

    print("== create devbox")
    dc("rm", task)  # best effort cleanup of a stale one
    rc, out, err = dc("new", "node22", task)
    check("dc new rc=0", rc == 0, (out or err)[:200])
    if rc != 0:
        summary()
        return 1

    print("== inside devbox")
    rc, out, err = dc("exec", task, "--", "sh", "-lc", "id -u; pwd; node -v")
    check("exec rc=0", rc == 0, (out or err)[:200])
    print("   " + out.replace("\n", " / "))

    rc, out, err = dc("exec", task, "--", "sh", "-lc",
                      "grep -E 'CapEff|NoNewPrivs' /proc/self/status")
    check("NoNewPrivs=1", "NoNewPrivs:\t1" in out, out.replace("\n", " "))
    check("CapEff all dropped", "CapEff:\t0000000000000000" in out, out.replace("\n", " "))

    rc, out, err = dc("exec", task, "--", "sh", "-lc",
                      "ls -l /var/run/docker.sock 2>&1 | head -1")
    check("docker.sock not visible", "No such file" in out, out[:120])

    rc, out, err = dc("exec", task, "--", "sh", "-lc",
                      "wget -qO- --timeout=3 http://172.17.0.1:47123/v1/health 2>&1 | head -c 120")
    ok = rc != 0 or "unauthorized" in out.lower() or "401" in out
    check("devbox cannot use the controller API", ok, out[:120])

    print("== workspace writable + git errors")
    rc, out, err = dc("exec", task, "--", "sh", "-lc",
                      "echo hi > /workspace/from-devbox.txt && cat /workspace/from-devbox.txt")
    check("devbox can write the workspace", rc == 0 and "hi" in out, (out or err)[:120])
    rc, out, err = dc("push", task)
    check("push on a non-repo gives a clear error",
          rc == 1 and "not a git repository" in out, (out or err)[:160])

    print("== git inside the devbox")
    rc, out, err = dc("exec", task, "--", "sh", "-lc", "git --version")
    check("git is installed (template packages)", rc == 0 and "git version" in out,
          (out or err)[:160])
    rc, out, err = dc("exec", task, "--", "sh", "-lc",
                      "git config --get user.email; git config --get credential.helper")
    check("identity and helper configured", "@" in out and "credential-devbox" in out,
          out.replace(chr(10), " / ")[:160])
    rc, out, err = dc("exec", task, "--", "sh", "-lc",
                      "test -r /etc/devbox-git-token && echo readable")
    check("token file readable by the devbox user", "readable" in out, (out or err)[:120])
    rc, out, err = dc("exec", task, "--", "sh", "-lc",
                      "git clone --depth 1 -q https://github.com/sysnapse/devbox-controller /workspace/dc && echo cloned")
    check("git clone works inside the devbox", "cloned" in out, (out or err)[:200])
    rc, out, err = dc("exec", task, "--", "sh", "-lc",
                      "cd /workspace/dc && git checkout -q -b probe && git commit -q --allow-empty -m probe && git log --oneline -1")
    check("git commit works inside the devbox", rc == 0 and "probe" in out, (out or err)[:200])

    print("== cleanup")
    rc, out, err = dc("rm", task)
    check("dc rm rc=0", rc == 0, (out or err)[:200])
    _ = json
    return summary()


def summary():
    print("all passed" if not fails else "failed %d: %s" % (len(fails), ", ".join(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
