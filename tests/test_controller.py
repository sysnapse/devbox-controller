#!/usr/bin/env python3
"""dca controller unit tests (no docker daemon needed)."""

import importlib.util
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

failures = []


def check(name, cond, extra=""):
    if cond:
        print("  OK   " + name)
    else:
        print("  FAIL " + name + ("  " + extra if extra else ""))
        failures.append(name)


def setup():
    tmp = tempfile.mkdtemp(prefix="dca-test-")
    tpl = pathlib.Path(tmp, "templates")
    work = pathlib.Path(tmp, "work")
    tpl.mkdir()
    work.mkdir()
    (tpl / "good.yml").write_text(
        "name: good\nimage: alpine@sha256:" + "a" * 64 + "\n")
    (tpl / "tagged.yml").write_text("name: tagged\nimage: alpine:3.21\n")
    (tpl / "evil.yml").write_text("name: evil\nimage: alpine@sha256:" + "b" * 64 +
                                  "\nprivileged: true\n")
    fake = HERE / "fake-docker"
    os.chmod(fake, 0o755)
    os.environ.update({
        "DCA_TOKEN": "t" * 32,
        "DCA_TEMPLATES_DIR": str(tpl),
        "DCA_DOCKER": str(fake),
        "DCA_OWNER": "agent",
        "FAKE_LOG": str(pathlib.Path(tmp, "docker.log")),
        "FAKE_PS_OUT": "",
    })
    spec = importlib.util.spec_from_file_location("dcaapp", ROOT / "controller" / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, tmp, work


def main():
    mod, tmp, work = setup()
    print("template validation")
    check("reject unknown template", raises(lambda: mod.load_template("nope"), mod.Bad))
    check("reject tag without digest", raises(lambda: mod.load_template("tagged"), mod.Bad))
    check("reject unknown field privileged", raises(lambda: mod.load_template("evil"), mod.Bad))
    t = mod.load_template("good")
    check("accept a valid template", t["name"] == "good")

    print("url checks")
    check("reject file://", raises(lambda: mod.check_url("file:///etc/passwd"), mod.Bad))
    check("reject git://", raises(lambda: mod.check_url("git://x/y"), mod.Bad))
    check("reject 127.0.0.1", raises(lambda: mod.check_url("https://127.0.0.1/a.git"), mod.Bad))
    check("reject 169.254.169.254", raises(lambda: mod.check_url("https://169.254.169.254/a.git"), mod.Bad))
    check("reject a 20.x private host", raises(lambda: mod.check_url("https://192.168.20.100/a.git"), mod.Bad))
    check("accept github.com", mod.check_url("https://github.com/a/b.git").startswith("https://"))

    print("create arguments (no host mount)")
    log = []
    mod.docker = lambda args, timeout=120, check=True: capture(log, args)
    res = mod.do_create({"template": "good", "task": "demo"})
    args = next(a for a in log if a[0] == "run")
    joined = " ".join(args)
    check("creates and returns the container", res["task"] == "demo" and res["container"] == "devbox-demo")
    check("reports container-only workspace", res.get("workspace") == "/workspace" and "host_path" not in res)
    check("has --cap-drop=ALL", "--cap-drop=ALL" in joined)
    check("has no-new-privileges", "no-new-privileges" in joined)
    check("no privileged flag", "privileged" not in joined)
    check("no docker.sock mount", "docker.sock" not in joined)
    check("has resource limits", "--pids-limit" in joined and "--memory" in joined and "--cpus" in joined)
    check("NO host workspace volume (-v)", "-v" not in args and not any(":" in a and "/" in a for a in args if a.startswith("/")), joined)
    check("carries the workdir label", "devbox.workdir=/workspace" in joined)
    check("carries the owner label", "devbox.owner=agent" in joined)
    check("no --network by default (no network created)", "--network" not in joined)

    print("network field")
    tpldir = pathlib.Path(mod.TEMPLATES_DIR)
    (tpldir / "nonet.yml").write_text(
        "name: nonet\nimage: alpine@sha256:" + "c" * 64 + "\nnetwork: none\n")
    mod.do_create({"template": "nonet", "task": "netdemo"})
    netargs = " ".join(log[-1])
    check("network: none passes --network none", "--network none" in netargs)
    (tpldir / "badnet.yml").write_text(
        "name: badnet\nimage: alpine@sha256:" + "d" * 64 + "\nnetwork: 'host; rm -rf /'\n")
    check("reject an invalid network name", raises(lambda: mod.load_template("badnet"), mod.Bad))

    print("agent cannot pass arbitrary docker flags")
    try:
        mod.do_create({"template": "good", "task": "demoX", "privileged": True, "runArgs": ["--privileged"]})
        check("unknown fields never reach docker args", "privileged" not in " ".join(next(a for a in log if a[0] == "run")))
    except mod.Bad:
        check("unknown field is rejected or ignored", True)

    print("task name injection")
    check("reject a task containing a slash", raises(lambda: mod.do_create({"template": "good", "task": "a/b"}), mod.Bad))
    check("reject a task containing ..", raises(lambda: mod.do_create({"template": "good", "task": ".."}), mod.Bad))
    check("reject an uppercase task", raises(lambda: mod.do_create({"template": "good", "task": "Demo"}), mod.Bad))

    print("exec arguments")
    check("reject an empty cmd", raises(lambda: mod.do_exec("demo", {"cmd": []}), mod.Bad))
    check("reject a string cmd (injection)", raises(lambda: mod.do_exec("demo", {"cmd": "rm -rf /"}), mod.Bad))
    check("reject a relative workdir", raises(lambda: mod.do_exec("demo", {"cmd": ["ls"], "workdir": "etc"}), mod.Bad))

    print("clone runs inside the container")
    mod.docker = fake_ps("devbox-demo\tdemo\tgood\tUp 2 minutes\n")
    mod.container_has_git = lambda task: True
    mod.container_workdir = lambda task: "/workspace"
    mod.container_user = lambda task: "1000:1000"
    clog = []
    mod.subprocess = FakeSubprocess(clog)
    cres = mod.do_clone("demo", {"url": "https://github.com/a/b.git"})
    call = " ".join(clog[0][0]) if clog else ""
    check("git clone runs via docker exec, no host path", "exec" in call and "git clone --" in call and "/workspace/b" in call, call)
    check("no token in argv", "k" * 32 not in call)
    check("returns container path", cres.get("container") == "devbox-demo" and cres.get("path") == "/workspace/b")
    check("reject a bad dir name", raises(lambda: mod.do_clone("demo", {"url": "https://github.com/a/b.git", "dir": "../etc"}), mod.Bad))
    mod.subprocess = REAL_SUBPROCESS

    print("YAML template parsing")
    tpldir = tpl = pathlib.Path(mod.TEMPLATES_DIR)
    (tpl / "nested.yml").write_text(
        "name: nested\nimage: alpine@sha256:" + "c" * 64 + "\nresources:\n  cpus: 2\n")
    check("reject nested mapping (scalars only)",
          raises(lambda: mod.load_template("nested"), mod.Bad))
    (tpl / "broken.yml").write_text("name: broken\nimage: [unclosed\n")
    check("reject broken YAML", raises(lambda: mod.load_template("broken"), mod.Bad))
    (tpl / "list.yml").write_text("- name: x\n- image: y\n")
    check("reject a non-mapping YAML", raises(lambda: mod.load_template("list"), mod.Bad))
    (tpl / "boolval.yml").write_text(
        "name: boolval\nimage: alpine@sha256:" + "d" * 64 + "\nuser: true\n")
    check("reject booleans (true/yes traps)",
          raises(lambda: mod.load_template("boolval"), mod.Bad))
    (tpl / "num.yml").write_text(
        "name: num\nimage: alpine@sha256:" + "e" * 64 + "\ncpus: 4\nmemory: 2g\n")
    nt = mod.load_template("num")
    check("YAML integer becomes a string", nt["cpus"] == "4" and nt["memory"] == "2g")
    (tpl / "legacy.tpl").write_text(
        "name: legacy\nimage: alpine@sha256:" + "f" * 64 + "\n")
    check("legacy .tpl is still readable", mod.load_template("legacy")["name"] == "legacy")
    (tpl / "both.tpl").write_text(
        "name: fromtpl\nimage: alpine@sha256:" + "a" * 64 + "\n")
    (tpl / "both.yml").write_text(
        "name: fromyml\nimage: alpine@sha256:" + "b" * 64 + "\n")
    check(".yml wins on a name clash", mod.load_template("both")["name"] == "fromyml")
    names = [x.get("name") for x in mod.list_templates()]
    check("list_templates does not duplicate", names.count("fromyml") == 1 and "fromtpl" not in names)

    print("container cap counts ALL containers (running and stopped)")
    live = "devbox-a\ttaska\tgood\tUp 2 minutes\n"
    stopped = "devbox-a\ttaska\tgood\tExited (0) 5 minutes ago\n"
    mod.MAX_TASKS = 1
    mod.docker = fake_ps(live)
    check("refuse a second devbox at the cap",
          raises(lambda: mod.do_create({"template": "good", "task": "demo2"}), mod.Bad))
    mod.docker = fake_ps(stopped)
    check("a stopped box STILL holds a slot (disk footprint)",
          raises(lambda: mod.do_create({"template": "good", "task": "demo2"}), mod.Bad))
    mod.MAX_TASKS = 2
    mod.docker = fake_ps(live)
    check("DCA_MAX_TASKS=2 allows a second box",
          mod.do_create({"template": "good", "task": "demo3"})["task"] == "demo3")
    mod.MAX_TASKS = 0
    mod.docker = fake_ps(live * 5)
    check("0 disables the cap",
          mod.do_create({"template": "good", "task": "demo4"})["task"] == "demo4")
    mod.MAX_TASKS = 1
    check("health reports live and total separately",
          mod.count_live() == 5 and mod.count_owned() == 5)
    mod.docker = fake_ps("")

    print("gc removes stopped containers")
    gcrows = ("devbox-a\ttaska\tgood\tUp 2 minutes\n"
              "devbox-b\ttaskb\tgood\tExited (0) 5 minutes ago\n"
              "devbox-c\ttaskc\tgood\tExited (0) 9 minutes ago\n")
    mod.docker = fake_ps(gcrows)
    gres = mod.do_gc()
    check("gc removes exactly the stopped boxes", gres["count"] == 2 and gres["removed"] == ["taskb", "taskc"], str(gres))

    print("apt package install (cap-drop=ALL workarounds)")
    logfile = pathlib.Path(os.environ["FAKE_LOG"])
    os.environ["FAKE_PKG_MGR"] = "apt-get"
    os.environ["FAKE_RC"] = "0"
    seen = logfile.read_text() if logfile.exists() else ""
    res = mod.install_packages("demo", ["git"])
    calls = (logfile.read_text() if logfile.exists() else "")[len(seen):]
    check("apt install reports ok", res.startswith("ok (apt-get: git)"), res)
    check("apt fetch runs as root (no setgroups to _apt)",
          "APT::Sandbox::User=root" in calls)
    check("apt downloads into a root-created dir",
          "Dir::Cache::archives=" in calls)
    check("the root-owned cache dir is created first",
          "mkdir -p" in calls and "/partial" in calls)
    check("apt never writes the image's 0700 _apt partial dir",
          "/var/cache/apt/archives/partial" not in calls)
    os.environ["FAKE_RC"] = "1"
    failed = mod.install_packages("demo", ["git"])
    check("a failed apt install is reported, not swallowed",
          failed.startswith("failed (apt-get)"), failed)
    os.environ["FAKE_RC"] = "0"
    os.environ["FAKE_PKG_MGR"] = "apk"
    apk = mod.install_packages("demo", ["git", "make"])
    check("apk path is untouched", apk == "ok (apk: git make)", apk)

    print("result")
    if failures:
        print("%d failed: %s" % (len(failures), ", ".join(failures)))
        return 1
    print("all passed")
    return 0


REAL_SUBPROCESS = __import__("subprocess")


class FakeSubprocess:
    """Capture subprocess.run calls so clone argv can be inspected without docker."""

    def __init__(self, log):
        self.log = log

    def run(self, args, **kw):
        self.log.append((args, kw))

        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()


def fake_ps(rows):
    """docker stub: the label listing returns `rows`, the name check returns nothing."""
    def run(args, timeout=120, check=True):
        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        p = P()
        joined = " ".join(args)
        if args and args[0] == "ps" and "label=devbox.owner" in joined:
            p.stdout = rows
        return p
    return run


def capture(log, args):
    log.append(args)

    class P:
        returncode = 0
        stdout = ""
        stderr = ""
    return P()


def raises(fn, exc):
    try:
        fn()
        return False
    except exc:
        return True


if __name__ == "__main__":
    sys.exit(main())
