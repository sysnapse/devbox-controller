#!/usr/bin/env python3
"""空挂载目录时，内置模板副本（/opt/templates 之类的 fallback）必须仍然可用。

回归场景（2026-11-15 实测遇到）：compose 在 ~/workspaces/dca 构建，
但挂载的 ./templates 在运行目录不存在 → 容器内 /templates 为空
→ GET /v1/templates 回 []，所有 devbox 都建不出来。
"""
import importlib.util
import os
import pathlib
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("dcaapp", ROOT / "controller" / "app.py")
mod = importlib.util.module_from_spec(spec)

fails = []


def check(label, cond):
    print(("  OK   " if cond else "  FAIL ") + label)
    if not cond:
        fails.append(label)


tmp = pathlib.Path(tempfile.mkdtemp(prefix="dca-fb-"))
empty = tmp / "mount"
empty.mkdir()
builtin = tmp / "builtin"
builtin.mkdir()
(builtin / "node22.yml").write_text("name: node22\nimage: alpine@sha256:" + "a" * 64 + "\n")
(empty / "override.yml").write_text("name: override\nimage: alpine@sha256:" + "b" * 64 + "\n")

os.environ.update({
    "DCA_TOKEN": "t" * 32,
    "DCA_TEMPLATES_DIR": str(empty),
    "DCA_TEMPLATES_DIR_FALLBACK": str(builtin),
    "DCA_WORK_ROOT": str(tmp / "work"),
    "DCA_OWNER": "agent",
})
spec.loader.exec_module(mod)

names = {t.get("name") for t in mod.list_templates()}
check("空挂载目录时仍列出内置模板", "node22" in names)
check("挂载目录的模板也在", "override" in names)
check("load_template 能走 fallback", mod.load_template("node22")["image"].endswith("a" * 64))

missing_fb = tmp / "does-not-exist"
mod.TEMPLATES_FALLBACK = str(missing_fb)
check("fallback 不存在时不炸", {t.get("name") for t in mod.list_templates()} == {"override"})
try:
    mod.load_template("node22")
    check("fallback 不存在时报 template 不存在", False)
except mod.Bad as exc:
    check("fallback 不存在时报 template 不存在", "template 不存在" in str(exc))

print("全部通过" if not fails else "失败：" + ", ".join(fails))
raise SystemExit(0 if not fails else 1)
