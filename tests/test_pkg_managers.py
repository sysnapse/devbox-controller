#!/usr/bin/env python3
"""Package-manager detection tests: apk, apt-get, dnf and none (fake docker)."""

import importlib.util
import os
import pathlib
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


def load():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="dca-pkg-"))
    tpl = tmp / "templates"
    work = tmp / "work"
    tpl.mkdir()
    work.mkdir()
    (tpl / "t.yml").write_text(
        "name: t\nimage: alpine@sha256:" + "a" * 64 + "\npackages: [git, build-essential]\n")
    fake = HERE / "fake-docker"
    os.chmod(fake, 0o755)
    log = tmp / "docker.log"
    os.environ.update({
        "DCA_TOKEN": "t" * 32,
        "DCA_TEMPLATES_DIR": str(tpl),
        "DCA_DOCKER": str(fake),
        "DCA_OWNER": "agent",
        "FAKE_LOG": str(log),
        "DCA_INJECT_GIT": "0",
    })
    spec = importlib.util.spec_from_file_location("dcaapp", ROOT / "controller" / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, log


def main():
    mod, log = load()
    print("package manager detection")

    os.environ["FAKE_PKG_MGR"] = "apk"
    check("apk detected", mod.pkg_manager("t1") == "apk")
    check("apk install ok", mod.install_packages("t1", ["git"]) == "ok (apk: git)")
    check("apk command recorded",
          any("apk add --no-cache git" in line for line in log.read_text().splitlines()))

    os.environ["FAKE_PKG_MGR"] = "apt-get"
    log.write_text("")
    check("apt-get detected", mod.pkg_manager("t2") == "apt-get")
    res = mod.install_packages("t2", ["git", "build-essential"])
    check("apt-get install ok", res == "ok (apt-get: git build-essential)", res)
    body = log.read_text()
    check("apt-get updates the index first", "update -qq" in body and "apt-get" in body)
    check("apt-get avoids recommends", "--no-install-recommends" in body)
    check("apt-get leaves no lists behind", "rm -rf /var/lib/apt/lists/*" in body)

    os.environ["FAKE_PKG_MGR"] = "dnf"
    log.write_text("")
    check("dnf detected", mod.pkg_manager("t3") == "dnf")
    check("dnf install ok", mod.install_packages("t3", ["git"]) == "ok (dnf: git)")

    os.environ["FAKE_PKG_MGR"] = "none"
    check("no manager detected", mod.pkg_manager("t4") == "")
    res = mod.install_packages("t4", ["git"])
    check("missing manager is explained", res.startswith("failed: no package manager"), res)

    check("empty package list is a no-op", mod.install_packages("t5", []) == "none")

    print("template package names")
    for name in ("git", "build-essential", "python3-dev", "cmake", "g++", "lua5.4", "musl-dev"):
        check("accepts " + name, mod.key_is_pkgs([name]))
    for bad in ("git; rm -rf /", "git && curl", "$(id)", "a b"):
        check("rejects " + bad, not mod.key_is_pkgs([bad]))
    check("rejects more than 12 packages", not mod.key_is_pkgs(["p%d" % i for i in range(13)]))

    print("result")
    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    print("all package-manager tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
