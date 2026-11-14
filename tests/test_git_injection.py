#!/usr/bin/env python3
"""Git-inside-devbox provisioning tests (fake docker, no daemon needed)."""

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


def load(inject="1", token="tok-abc"):
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="dca-git-"))
    tpl = tmp / "templates"
    work = tmp / "work"
    tpl.mkdir()
    work.mkdir()
    (tpl / "tt.yml").write_text(
        "name: tt\nimage: alpine@sha256:" + "a" * 64 + "\npackages: [git, curl]\n")
    (tpl / "nopkg.yml").write_text("name: nopkg\nimage: alpine@sha256:" + "c" * 64 + "\n")
    (tpl / "badpkg.yml").write_text(
        "name: badpkg\nimage: alpine@sha256:" + "d" * 64 + "\npackages: [git; rm -rf /]\n")
    (tpl / "scalarpkg.yml").write_text(
        "name: scalarpkg\nimage: alpine@sha256:" + "e" * 64 + "\npackages: git\n")
    fake = HERE / "fake-docker"
    os.chmod(fake, 0o755)
    log = tmp / "docker.log"
    os.environ.update({
        "DCA_TOKEN": "t" * 32,
        "DCA_TEMPLATES_DIR": str(tpl),
        "DCA_WORK_ROOT": str(work),
        "DCA_DOCKER": str(fake),
        "DCA_OWNER": "agent",
        "DCA_GIT_TOKEN": token,
        "DCA_INJECT_GIT": inject,
        "DCA_GIT_NAME": "sysnapse",
        "DCA_GIT_EMAIL": "8198069+sysnapse@users.noreply.github.com",
        "FAKE_LOG": str(log),
        "FAKE_PS_OUT": "",
    })
    spec = importlib.util.spec_from_file_location("dcagit", ROOT / "controller" / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, tmp, log


def raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


def main():
    mod, tmp, log = load()
    print("template packages field")
    t = mod.load_template("tt")
    check("accept packages list", t["packages"] == ["git", "curl"])
    check("template without packages", mod.load_template("nopkg")["packages"] == [])
    check("reject shell injection in package name",
          raises(lambda: mod.load_template("badpkg"), mod.Bad))
    check("reject scalar packages", raises(lambda: mod.load_template("scalarpkg"), mod.Bad))

    print("create provisions git")
    res = mod.do_create({"task": "t1", "template": "tt"})
    calls = log.read_text(encoding="utf-8").splitlines()
    run = [c for c in calls if c.startswith("run ")]
    check("docker run called once", len(run) == 1, str(len(run)))
    check("identity env passed", "-e GIT_AUTHOR_NAME=sysnapse" in run[0])
    check("email env passed", "-e GIT_AUTHOR_EMAIL=8198069+sysnapse@users.noreply.github.com" in run[0])
    check("token never in docker argv", "tok-abc" not in " ".join(calls))
    check("apk add ran", any(c.startswith("exec -u 0 devbox-t1 sh -c apk add --no-cache git curl") for c in calls))
    check("credential helper written",
          any("cat > /usr/local/bin/git-credential-devbox" in c for c in calls))
    check("gitconfig written", any("cat > /etc/gitconfig" in c for c in calls))
    check("token file written", any("cat > /etc/devbox-git-token" in c for c in calls))
    check("response reports git ok", res["git"] == "ok (identity + token)", res["git"])
    check("response reports packages ok", str(res["packages"]).startswith("ok (apk:"),
          res["packages"])

    print("DCA_INJECT_GIT=0 keeps the token on the controller side")
    mod2, tmp2, log2 = load(inject="0")
    res2 = mod2.do_create({"task": "t2", "template": "nopkg"})
    calls2 = log2.read_text(encoding="utf-8").splitlines()
    check("no token file", not any("devbox-git-token" in c for c in calls2))
    check("no gitconfig", not any("/etc/gitconfig" in c for c in calls2))
    check("state says skipped", res2["git"].startswith("skipped"), res2["git"])

    print("git missing in template")
    mod3, tmp3, log3 = load()
    mod3.container_has_git = lambda task: False
    res3 = mod3.do_create({"task": "t3", "template": "nopkg"})
    check("state explains missing git", "git not installed" in res3["git"], res3["git"])

    print()
    if failures:
        print("FAILED: " + ", ".join(failures))
        sys.exit(1)
    print("all git-injection tests passed")


if __name__ == "__main__":
    main()
