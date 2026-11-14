#!/usr/bin/env python3
"""Every template shipped in the repo must load: names, digests, packages, YAML."""

import importlib.util
import os
import pathlib
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
TEMPLATES = ROOT / "templates"

failures = []


def check(name, cond, extra=""):
    if cond:
        print("  OK   " + name)
    else:
        print("  FAIL " + name + ("  " + extra if extra else ""))
        failures.append(name)


def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="dca-all-"))
    work = tmp / "work"
    work.mkdir()
    os.environ.update({
        "DCA_TOKEN": "t" * 32,
        "DCA_TEMPLATES_DIR": str(TEMPLATES),
        "DCA_WORK_ROOT": str(work),
        "DCA_DOCKER": str(HERE / "fake-docker"),
        "DCA_OWNER": "agent",
        "DCA_INJECT_GIT": "0",
        "FAKE_LOG": str(tmp / "docker.log"),
    })
    spec = importlib.util.spec_from_file_location("dcaapp", ROOT / "controller" / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    files = sorted(p.stem for p in TEMPLATES.glob("*.yml"))
    print("repo templates (%d)" % len(files))
    check("at least 20 templates ship", len(files) >= 20, str(len(files)))

    listed = mod.list_templates()
    bad = [t for t in listed if t.get("error")]
    check("every template parses", not bad, str(bad)[:300])
    names = [t["name"] for t in listed if not t.get("error")]
    check("no duplicate names", len(names) == len(set(names)))
    check("list matches files", sorted(names) == files, str(names))

    print("template invariants")
    for t in listed:
        if t.get("error"):
            continue
        tag = t["name"]
        check(tag + " pins a digest", "@sha256:" in t["image"])
        check(tag + " has no tag left", "@" in t["image"] and
              t["image"].split("@")[0].count(":") <= 1, t["image"])
        check(tag + " runs as uid 1000", t["user"] == "1000:1000")
        check(tag + " has a memory limit", t["memory"].endswith("g"))
        check(tag + " installs git", "git" in t["packages"], str(t["packages"]))
        check(tag + " notes explain the image", len(t["notes"]) > 10)
        check(tag + " workdir is absolute", t["workdir"].startswith("/"))

    print("result")
    if failures:
        print("FAILED: %d" % len(failures))
        return 1
    print("all repo templates valid (%d)" % len(files))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
