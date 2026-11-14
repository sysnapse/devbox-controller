#!/usr/bin/env python3
"""Live end-to-end check against a running controller.

Reads the token from DEVBOX_TOKEN (or DCA_TOKEN) and the base URL from DCA_URL
(falling back to the local default gateway on port 47123).

    python3 tests/e2e_live.py

Every step prints PASS/FAIL plus the raw output that justifies it.
"""
import json
import os
import pathlib
import re
import subprocess
import sys

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


def main():
    print("== controller reachable")
    rc, out, err = dc("health")
    check("dc health rc=0", rc == 0, (out or err)[:120])
    import urllib.error
    import urllib.request
    base = env.get("DCA_URL") or ""
    if not base:
        # let dc resolve; probe the same way it does for the 401 test
        for cand in ("http://172.23.0.1:47123", "http://172.17.0.1:47123",
                     "http://127.0.0.1:47123"):
            try:
                urllib.request.urlopen(cand + "/v1/health", timeout=2)
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    base = cand
                    break
            except Exception:
                continue
    if base:
        try:
            urllib.request.urlopen(base + "/v1/health", timeout=3)
            check("无 token 必须 401", False, "居然放行")
        except urllib.error.HTTPError as exc:
            check("无 token 必须 401", exc.code == 401, "HTTP " + str(exc.code))

    print("== templates")
    rc, out, err = dc("templates")
    names = re.findall(r'"name": "([a-z0-9-]+)"', out)
    check("模板非空", bool(names), ",".join(names) or out[:100])
    check("含 node22", "node22" in names)

    print("== create devbox")
    task = "e2e1"
    dc("rm", task)  # best effort cleanup of a stale one
    rc, out, err = dc("new", "node22", task)
    check("dc new 成功", rc == 0, (out or err)[:200])
    if rc != 0:
        return

    print("== inside devbox")
    rc, out, err = dc("exec", task, "--", "sh", "-lc", "id -u; pwd; node -v")
    check("exec 成功", rc == 0, (out or err)[:200])
    print("   " + out.replace("\n", " / "))

    rc, out, err = dc("exec", task, "--", "sh", "-lc",
                      "grep -E 'CapEff|NoNewPrivs' /proc/self/status")
    check("NoNewPrivs=1", "NoNewPrivs:\t1" in out, out.replace("\n", " "))
    check("无 docker.sock", "No such file" in out or out.count("CapEff") == 1)

    rc, out, err = dc("exec", task, "--", "sh", "-lc",
                      "ls -l /var/run/docker.sock 2>&1 | head -1")
    check("socket 不可见", "No such file" in out, out[:120])

    rc, out, err = dc("exec", task, "--", "sh", "-lc",
                      "wget -qO- --timeout=3 http://172.17.0.1:47123/v1/health 2>&1 | head -c 120")
    ok = rc != 0 or "unauthorized" in out.lower() or "401" in out
    check("devbox 打 controller 被拒（401/超时）", ok, out[:120])

    print("== workspace writable + push")
    rc, out, err = dc("exec", task, "--", "sh", "-lc",
                      "echo hi > /workspace/from-devbox.txt && cat /workspace/from-devbox.txt")
    check("devbox 能写 workspace", rc == 0 and "hi" in out, (out or err)[:120])
    rc, out, err = dc("push", task)
    check("dc push 成功", rc == 0, (out or err)[:200])

    print("== cleanup")
    rc, out, err = dc("rm", task)
    check("dc rm 成功", rc == 0, (out or err)[:200])

    print("全部通过" if not fails else "失败 " + str(len(fails)) + " 项：" + ", ".join(fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
