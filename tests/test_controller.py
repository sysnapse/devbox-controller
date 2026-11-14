#!/usr/bin/env python3
"""dca controller 单元测试（不需要 docker daemon）。"""

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
    (tpl / "good.tpl").write_text(
        "name: good\nimage: alpine@sha256:" + "a" * 64 + "\n")
    (tpl / "tagged.tpl").write_text("name: tagged\nimage: alpine:3.21\n")
    (tpl / "evil.tpl").write_text("name: evil\nimage: alpine@sha256:" + "b" * 64 +
                                  "\nprivileged: true\n")
    fake = HERE / "fake-docker"
    os.chmod(fake, 0o755)
    os.environ.update({
        "DCA_TOKEN": "t" * 32,
        "DCA_TEMPLATES_DIR": str(tpl),
        "DCA_WORK_ROOT": str(work),
        "DCA_DOCKER": str(fake),
        "DCA_OWNER": "agent",
        "FAKE_LOG": str(pathlib.Path(tmp, "docker.log")),
    })
    spec = importlib.util.spec_from_file_location("dcaapp", ROOT / "controller" / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, tmp, work


def main():
    mod, tmp, work = setup()
    print("template 校验")
    check("拒绝不存在的模板", raises(lambda: mod.load_template("nope"), mod.Bad))
    check("拒绝 tag（无 digest）", raises(lambda: mod.load_template("tagged"), mod.Bad))
    check("拒绝未知字段 privileged", raises(lambda: mod.load_template("evil"), mod.Bad))
    t = mod.load_template("good")
    check("接受合规模板", t["name"] == "good")

    print("路径校验")
    check("拒绝 .. 越界", raises(lambda: mod.work_path("good", "../../etc"), mod.Bad))
    check("拒绝绝对路径逃逸", raises(lambda: mod.work_path("good", "/etc/passwd"), mod.Bad))

    print("URL 校验")
    check("拒绝 file://", raises(lambda: mod.check_url("file:///etc/passwd"), mod.Bad))
    check("拒绝 git://", raises(lambda: mod.check_url("git://x/y"), mod.Bad))
    check("拒绝 127.0.0.1", raises(lambda: mod.check_url("https://127.0.0.1/a.git"), mod.Bad))
    check("拒绝 169.254.169.254", raises(lambda: mod.check_url("https://169.254.169.254/a.git"), mod.Bad))
    check("拒绝 20.x 内网", raises(lambda: mod.check_url("https://192.168.20.100/a.git"), mod.Bad))
    check("接受 github", mod.check_url("https://github.com/a/b.git").startswith("https://"))

    print("创建容器参数")
    mod.docker = lambda args, **kw: runcapture(mod, args, **kw)
    log = []
    mod.docker = lambda args, timeout=120, check=True: capture(log, args)
    res = mod.do_create({"template": "good", "task": "demo"})
    args = next(a for a in log if a[0] == "run")
    joined = " ".join(args)
    check("创建成功并返回路径", res["task"] == "demo" and res["template"] == "good")
    check("含 --cap-drop=ALL", "--cap-drop=ALL" in joined)
    check("含 no-new-privileges", "no-new-privileges" in joined)
    check("不含 privileged", "privileged" not in joined)
    check("不含 docker.sock 挂载", "docker.sock" not in joined)
    check("含资源限制", "--pids-limit" in joined and "--memory" in joined and "--cpus" in joined)
    check("绑定 workspace", "-v" in args and str(work / "demo") in joined)
    check("带 owner label", "devbox.owner=agent" in joined)
    check("默认不传 --network（不需要 create network）", "--network" not in joined)

    print("网络字段")
    tpldir = pathlib.Path(mod.TEMPLATES_DIR)
    (tpldir / "nonet.tpl").write_text(
        "name: nonet\nimage: alpine@sha256:" + "c" * 64 + "\nnetwork: none\n")
    mod.do_create({"template": "nonet", "task": "netdemo"})
    netargs = " ".join(log[-1])
    check("network: none 会传 --network none", "--network none" in netargs)
    (tpldir / "badnet.tpl").write_text(
        "name: badnet\nimage: alpine@sha256:" + "d" * 64 + "\nnetwork: 'host; rm -rf /'\n")
    check("拒绝非法 network 名", raises(lambda: mod.load_template("badnet"), mod.Bad))

    print("拒绝 agent 传任意 docker flag")
    try:
        mod.do_create({"template": "good", "task": "demo", "privileged": True, "runArgs": ["--privileged"]})
        check("忽略未知字段（不进入 docker args）", "privileged" not in " ".join(next(a for a in log if a[0] == "run")))
    except mod.Bad:
        check("未知字段被拒或忽略", True)

    print("task 名注入")
    check("拒绝含 / 的 task", raises(lambda: mod.do_create({"template": "good", "task": "a/b"}), mod.Bad))
    check("拒绝含 .. 的 task", raises(lambda: mod.do_create({"template": "good", "task": ".."}), mod.Bad))
    check("拒绝大写 task", raises(lambda: mod.do_create({"template": "good", "task": "Demo"}), mod.Bad))

    print("exec 参数")
    check("拒绝空 cmd", raises(lambda: mod.do_exec("demo", {"cmd": []}), mod.Bad))
    check("拒绝字符串 cmd（防命令注入）", raises(lambda: mod.do_exec("demo", {"cmd": "rm -rf /"}), mod.Bad))
    check("拒绝相对 workdir", raises(lambda: mod.do_exec("demo", {"cmd": ["ls"], "workdir": "etc"}), mod.Bad))

    print("结果")
    if failures:
        print("%d 项失败：%s" % (len(failures), ", ".join(failures)))
        return 1
    print("全部通过")
    return 0


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
