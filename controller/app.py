#!/usr/bin/env python3
"""
dca controller —— 唯一持有 docker socket 的组件（controller 容器）。

三个角色用人类可读的名字：agent（呼叫方）、controller（本进程，建环境）、
devbox（被建出来的开发环境容器）。

设计要点：
  * agent 只能经 HTTP + Bearer token 呼叫本 API，拿不到 socket。
  * 模板由 owner 写死在 /templates，agent 只能点名，不能传任何 docker flag。
  * 所有容器带 label devbox.task / devbox.owner，只操作带该 label 的容器。
  * 只依赖 Python 标准库 + docker CLI，无第三方套件。
"""

import hmac
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# ---------------------------------------------------------------- 配置

TOKEN = os.environ.get("DCA_TOKEN", "")
TEMPLATES_DIR = os.environ.get("DCA_TEMPLATES_DIR", "/templates")
WORK_ROOT = os.environ.get("DCA_WORK_ROOT", "/work")
# 空字符串或 "default" = 不传 --network，完全不需要 docker network create。
# 要隔离可设 "none"；要指定已存在的网络就填名字。
DEFAULT_NETWORK = os.environ.get("DCA_NETWORK", "")
OWNER = os.environ.get("DCA_OWNER", "agent")
DOCKER = os.environ.get("DCA_DOCKER", "docker")
EXEC_TIMEOUT = int(os.environ.get("DCA_EXEC_TIMEOUT", "900"))
IDLE_HOURS = float(os.environ.get("DCA_IDLE_HOURS", "0"))
ALLOW_TAGS = os.environ.get("DCA_ALLOW_TAGS", "0") == "1"
ALLOW_PRIVATE_URL = os.environ.get("DCA_ALLOW_PRIVATE_URL", "0") == "1"
# 默认绑 0.0.0.0：agent 容器未必在 host 网络，要用 docker0 网关 172.17.0.1 才连得上。
# 认证完全靠 token，不靠绑定地址。
BIND = os.environ.get("DCA_BIND", "0.0.0.0")
PORT = int(os.environ.get("DCA_PORT", "47123"))

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
NET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

_last_used = {}
_lock = threading.Lock()


class Bad(Exception):
    """用户输入不合法（4xx）。"""


# ---------------------------------------------------------------- 模板

TPL_KEYS = {"name", "image", "workdir", "network", "cpus", "memory", "pids", "user", "notes"}


def load_template(name):
    if not NAME_RE.match(name or ""):
        raise Bad("template name 不合法")
    path = os.path.join(TEMPLATES_DIR, name + ".tpl")
    real = os.path.realpath(path)
    if not real.startswith(os.path.realpath(TEMPLATES_DIR) + os.sep):
        raise Bad("template 路径越界")
    if not os.path.isfile(real):
        raise Bad("template 不存在：" + name)
    tpl = {}
    with open(real, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                raise Bad("template 格式错误：" + name)
            k, v = line.split(":", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k not in TPL_KEYS:
                raise Bad("template 含有未知字段：" + k)
            tpl[k] = v
    for req in ("image",):
        if not tpl.get(req):
            raise Bad("template 缺少必填字段：" + req)
    if not NAME_RE.match(tpl.get("name", name)):
        raise Bad("template 内 name 不合法")
    img = tpl["image"]
    if not ALLOW_TAGS and not DIGEST_RE.search(img):
        raise Bad("template image 必须带 @sha256: digest（可用 DCA_ALLOW_TAGS=1 放宽）")
    tpl.setdefault("workdir", "/workspace")
    tpl.setdefault("network", DEFAULT_NETWORK)
    if tpl["network"] not in ("", "default") and not NET_RE.match(tpl["network"]):
        raise Bad("template network 不合法：" + tpl["network"])
    tpl.setdefault("cpus", "2")
    tpl.setdefault("memory", "4g")
    tpl.setdefault("pids", "512")
    tpl.setdefault("user", "1000:1000")
    if not tpl["workdir"].startswith("/"):
        raise Bad("template workdir 必须是绝对路径")
    tpl["_file"] = os.path.basename(real)
    return tpl


def list_templates():
    out = []
    if not os.path.isdir(TEMPLATES_DIR):
        return out
    for fn in sorted(os.listdir(TEMPLATES_DIR)):
        if fn.endswith(".tpl"):
            try:
                out.append(load_template(fn[:-4]))
            except Bad as exc:
                out.append({"name": fn[:-4], "error": str(exc)})
    return out


# ---------------------------------------------------------------- docker

def docker(args, timeout=120, check=True):
    cmd = [DOCKER] + args
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise Bad("docker 失败：%s" % (proc.stderr.strip() or proc.stdout.strip()))
    return proc


def cname(task):
    return "devbox-" + task


def require_task(task):
    if not NAME_RE.match(task or ""):
        raise Bad("task 不合法")
    return task


def work_path(task, sub=""):
    base = os.path.realpath(WORK_ROOT)
    path = os.path.realpath(os.path.join(base, task, sub))
    if not (path == os.path.join(base, task) or path.startswith(os.path.join(base, task) + os.sep)):
        raise Bad("路径越界")
    return path


def list_own():
    proc = docker(["ps", "-a", "--filter", "label=devbox.owner=" + OWNER,
                   "--format", "{{.Names}}\t{{.Label \"devbox.task\"}}\t{{.Label \"devbox.template\"}}\t{{.Status}}"])
    rows = []
    for line in proc.stdout.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        task = parts[1] if len(parts) > 1 else ""
        rows.append({
            "task": task,
            "template": parts[2] if len(parts) > 2 else "",
            "status": parts[3] if len(parts) > 3 else "",
        })
    return rows


def require_container(task):
    require_task(task)
    proc = docker(["ps", "-a", "--filter", "name=^%s$" % cname(task),
                   "--filter", "label=devbox.owner=" + OWNER, "--format", "{{.Names}}"], check=False)
    if not proc.stdout.strip():
        raise Bad("找不到属于本 owner 的容器：" + task)
    return cname(task)


# ---------------------------------------------------------------- 动作

def do_create(payload):
    task = require_task(payload.get("task", ""))
    tpl = load_template(payload.get("template", ""))
    work = work_path(task)
    try:
        os.makedirs(work, exist_ok=True)
    except OSError as exc:
        raise Bad("无法建立 workspace %s（uid=%d）：%s；请确认宿主目录已 chown 给该 uid"
                  % (work, os.getuid(), exc))
    os.chmod(work, 0o777)  # C 以非 root uid 跑；bind mount 沿用宿主 ownership，只放开该 task 目录
    existing = docker(["ps", "-a", "--filter", "name=^%s$" % cname(task),
                       "--format", "{{.Names}}"], check=False).stdout.strip()
    if existing:
        raise Bad("task 已存在：" + task)
    args = [
        "run", "-d", "--name", cname(task),
        "--label", "devbox.owner=" + OWNER,
        "--label", "devbox.task=" + task,
        "--label", "devbox.template=" + tpl["name"],
        "--cap-drop=ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", str(tpl["pids"]),
        "--memory", tpl["memory"],
        "--cpus", tpl["cpus"],
        "--user", tpl["user"],
        "-w", tpl["workdir"],
        "-v", work + ":" + tpl["workdir"],
        tpl["image"],
        "sleep", "infinity",
    ]
    net = tpl["network"]
    if net and net != "default":
        args[args.index("-w"):args.index("-w")] = ["--network", net]
    docker(args, timeout=180, check=True)
    touch(task)
    return {"task": task, "template": tpl["name"], "container": cname(task),
            "workdir": tpl["workdir"], "host_path": work}


def do_exec(task, payload):
    require_container(task)
    cmd = payload.get("cmd")
    if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd):
        raise Bad("cmd 必须是字符串数组")
    if len(cmd) > 64:
        raise Bad("cmd 太长")
    args = ["exec"]
    wd = payload.get("workdir")
    if wd:
        if not wd.startswith("/"):
            raise Bad("workdir 必须是绝对路径")
        args += ["-w", wd]
    args += ["-e", "TERM=dumb", cname(task)] + cmd
    timeout = min(int(payload.get("timeout", EXEC_TIMEOUT)), EXEC_TIMEOUT)
    try:
        proc = subprocess.run([DOCKER] + args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Bad("exec 逾时（%ss）" % timeout)
    touch(task)
    return {"rc": proc.returncode,
            "stdout": proc.stdout[-200000:],
            "stderr": proc.stderr[-50000:]}


def check_url(url):
    if not isinstance(url, str) or len(url) > 2000:
        raise Bad("url 不合法")
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "ssh"):
        raise Bad("只允许 https:// 或 ssh://（git:// file:// 一律禁止）")
    host = parsed.hostname or ""
    if not host:
        raise Bad("url 缺少 host")
    if parsed.scheme == "https" and not ALLOW_PRIVATE_URL:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None and (ip.is_private or ip.is_loopback or ip.is_link_local
                               or ip.is_reserved or ip.is_multicast):
            raise Bad("禁止内网地址")
        if host.lower() in ("localhost", "localhost.localdomain"):
            raise Bad("禁止内网地址")
    return url


def relax_perms(root):
    """把新写入的目录/文件放开到 a+rwX，避免 controller uid 与 devbox user 不同时写不进去。"""
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            os.chmod(dirpath, 0o777)
            for name in filenames:
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    continue
                mode = os.stat(full).st_mode
                os.chmod(full, mode | 0o666 if (mode & 0o111) == 0 else mode | 0o777)
    except OSError:
        pass


def do_clone(task, payload):
    require_container(task)
    url = check_url(payload.get("url"))
    sub = payload.get("dir", "")
    if sub and not NAME_RE.match(sub):
        raise Bad("dir 不合法")
    dest = work_path(task, sub or os.path.basename(urlparse(url).path).removesuffix(".git") or "repo")
    if os.path.exists(dest) and os.listdir(dest):
        raise Bad("目标目录非空：" + dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    proc = subprocess.run(["git", "clone", "--", url, dest], capture_output=True,
                          text=True, timeout=600, env=env)
    touch(task)
    if proc.returncode != 0:
        raise Bad("clone 失败：" + (proc.stderr.strip()[-2000:] or "unknown"))
    relax_perms(dest)
    return {"path": dest, "rel": os.path.relpath(dest, work_path(task))}


def do_push(task, payload):
    require_container(task)
    sub = payload.get("dir", "")
    dest = work_path(task, sub) if sub else work_path(task)
    if not os.path.isdir(os.path.join(dest, ".git")):
        raise Bad("不是 git 仓库：" + dest)
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    proc = subprocess.run(["git", "-C", dest, "push"], capture_output=True, text=True,
                          timeout=600, env=env)
    touch(task)
    if proc.returncode != 0:
        raise Bad("push 失败：" + (proc.stderr.strip()[-2000:] or "unknown"))
    relax_perms(dest)
    return {"stdout": proc.stdout[-5000:], "stderr": proc.stderr[-5000:]}


def do_stop(task):
    require_container(task)
    docker(["stop", cname(task)], timeout=60, check=False)
    return {"task": task, "stopped": True}


def do_rm(task):
    require_container(task)
    docker(["rm", "-f", "-v", cname(task)], timeout=60, check=False)
    with _lock:
        _last_used.pop(task, None)
    return {"task": task, "removed": True}


def touch(task):
    with _lock:
        _last_used[task] = time.time()


# ---------------------------------------------------------------- 回收

def reaper():
    if IDLE_HOURS <= 0:
        return
    while True:
        time.sleep(300)
        cutoff = time.time() - IDLE_HOURS * 3600
        for row in list_own():
            task = row["task"]
            with _lock:
                last = _last_used.get(task, 0)
            if last and last < cutoff:
                try:
                    docker(["stop", cname(task)], timeout=60, check=False)
                    print("[reaper] stopped idle task %s" % task, flush=True)
                except Exception as exc:  # noqa: BLE001
                    print("[reaper] %s: %s" % (task, exc), flush=True)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "dca/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _auth(self):
        if not TOKEN:
            return False
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Bearer "):
            return False
        return hmac.compare_digest(hdr[7:].strip(), TOKEN)

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            raise Bad("body 太大")
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            raise Bad("body 不是合法 JSON")
        if not isinstance(obj, dict):
            raise Bad("body 必须是 JSON object")
        return obj

    def _route(self, method):
        if not self._auth():
            self._send(401, {"error": "unauthorized"})
            return
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            payload = self._body() if method in ("POST", "DELETE") else {}
            if method == "GET" and path == "/v1/health":
                self._send(200, {"ok": True, "owner": OWNER})
            elif method == "GET" and path == "/v1/templates":
                self._send(200, {"templates": list_templates()})
            elif method == "GET" and path == "/v1/tasks":
                self._send(200, {"tasks": list_own()})
            elif method == "POST" and path == "/v1/tasks":
                self._send(201, do_create(payload))
            elif method == "POST" and path.startswith("/v1/tasks/"):
                parts = path.split("/")
                # /v1/tasks/<task>/<action>
                if len(parts) != 5:
                    raise Bad("路径不支持")
                task, action = require_task(parts[3]), parts[4]
                if action == "exec":
                    self._send(200, do_exec(task, payload))
                elif action == "clone":
                    self._send(200, do_clone(task, payload))
                elif action == "push":
                    self._send(200, do_push(task, payload))
                elif action == "stop":
                    self._send(200, do_stop(task))
                else:
                    raise Bad("未知 action：" + action)
            elif method == "DELETE" and path.startswith("/v1/tasks/"):
                parts = path.split("/")
                if len(parts) != 4:
                    raise Bad("路径不支持")
                self._send(200, do_rm(require_task(parts[3])))
            else:
                self._send(404, {"error": "not found"})
        except Bad as exc:
            self._send(400, {"error": str(exc)})
        except subprocess.TimeoutExpired:
            self._send(504, {"error": "upstream timeout"})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": "%s: %s" % (type(exc).__name__, exc)})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")


def main():
    if not TOKEN or len(TOKEN) < 16:
        print("DCA_TOKEN 未设置或长度 <16，拒绝启动", file=sys.stderr)
        return 2
    if not shutil.which(DOCKER):
        print("找不到 docker CLI", file=sys.stderr)
        return 2
    try:
        os.makedirs(WORK_ROOT, exist_ok=True)
        probe = os.path.join(WORK_ROOT, ".write-probe")
        with open(probe, "w") as fh:
            fh.write("ok")
        os.unlink(probe)
    except OSError as exc:
        print("WORK_ROOT 不可写：%s（uid=%d gid=%d，错误 %s）\n"
              "修法：在宿主执行 mkdir -p <WORK_ROOT> && chown <uid>:<gid> <WORK_ROOT>，"
              "或把 DCA_HOST_WORK_ROOT 指到一个该 uid 可写的目录。"
              % (WORK_ROOT, os.getuid(), os.getgid(), exc), file=sys.stderr)
        return 2
    threading.Thread(target=reaper, daemon=True).start()
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    print("dca controller listening on %s:%s (owner=%s, work_root=%s, network=%s)"
          % (BIND, PORT, OWNER, WORK_ROOT, DEFAULT_NETWORK or "(docker default)"), flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
