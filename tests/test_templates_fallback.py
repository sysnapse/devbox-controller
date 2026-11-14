#!/usr/bin/env python3
"""With an empty mount, the baked-in template copy must still be available.

Regression scene (hit in a live run on 2026-11-15): compose builds in ~/workspaces/dca
but the mounted ./templates does not exist in the run directory, so /templates inside
the container is empty: GET /v1/templates returns [] and no devbox can be created.
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
check("built-in templates listed with an empty mount", "node22" in names)
check("mounted templates are listed too", "override" in names)
check("load_template falls back", mod.load_template("node22")["image"].endswith("a" * 64))

missing_fb = tmp / "does-not-exist"
mod.TEMPLATES_FALLBACK = str(missing_fb)
check("no crash when the fallback is missing", {t.get("name") for t in mod.list_templates()} == {"override"})
try:
    mod.load_template("node22")
    check("missing fallback reports no such template", False)
except mod.Bad as exc:
    check("missing fallback reports no such template", "no such template" in str(exc))

print("all passed" if not fails else "failed: " + ", ".join(fails))
raise SystemExit(0 if not fails else 1)
