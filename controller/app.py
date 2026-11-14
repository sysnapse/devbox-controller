#!/usr/bin/env python3
"""
devbox controller - the only component that holds the docker socket.

Three roles, each with a human-readable name: agent (the caller), controller
(this process, which builds environments), devbox (the container it builds).

Design notes:
  * The agent calls this API over HTTP with a Bearer token; it never gets the socket.
  * Templates are fixed by the owner in /templates (plain YAML). The agent names one
    and cannot pass any docker flag.
  * Every container carries devbox.task / devbox.owner labels; only labelled ones are touched.
  * Depends on the Python standard library, PyYAML and the docker CLI only.
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

import yaml

# --------------------------------------------------------------- config

TOKEN = os.environ.get("DCA_TOKEN", "")
TEMPLATES_DIR = os.environ.get("DCA_TEMPLATES_DIR", "/templates")
# Templates baked into the image at build time. Used when the mounted directory
# is empty or missing, so the API never reports zero templates.
TEMPLATES_FALLBACK = os.environ.get("DCA_TEMPLATES_DIR_FALLBACK", "/opt/templates")
WORK_ROOT = os.environ.get("DCA_WORK_ROOT", "/work")
# Empty or "default" = no --network flag, so no docker network create is needed.
# Set "none" to isolate; to attach an existing network, put its name here.
DEFAULT_NETWORK = os.environ.get("DCA_NETWORK", "")
OWNER = os.environ.get("DCA_OWNER", "agent")
DOCKER = os.environ.get("DCA_DOCKER", "docker")
EXEC_TIMEOUT = int(os.environ.get("DCA_EXEC_TIMEOUT", "900"))
IDLE_HOURS = float(os.environ.get("DCA_IDLE_HOURS", "0"))
ALLOW_TAGS = os.environ.get("DCA_ALLOW_TAGS", "0") == "1"
ALLOW_PRIVATE_URL = os.environ.get("DCA_ALLOW_PRIVATE_URL", "0") == "1"
# Optional git credentials for clone/push, kept on the host side only.
# The token never reaches the devbox: it is read from this process environment by
# a git credential helper, so it never appears in argv or in a .git/config remote.
GIT_TOKEN = os.environ.get("DCA_GIT_TOKEN", "")
GIT_USER = os.environ.get("DCA_GIT_USER", "x-access-token")
# Binds 0.0.0.0 by default: the agent may sit on a bridge, where the only route in
# is the bridge gateway. Authentication is the token, not the bind address.
BIND = os.environ.get("DCA_BIND", "0.0.0.0")
PORT = int(os.environ.get("DCA_PORT", "47123"))

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
NET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

_last_used = {}
_lock = threading.Lock()


class Bad(Exception):
    """Invalid input from the caller (4xx)."""


# ------------------------------------------------------------ templates

TPL_KEYS = {"name", "image", "workdir", "network", "cpus", "memory", "pids", "user", "notes"}
# A template is plain YAML (templates/<name>.yml). Legacy *.tpl files are still
# accepted while upgrading; .yml wins on a name clash, so renaming can be gradual.
TPL_EXTS = (".yml", ".tpl")


def template_dirs():
    """Template directories in priority order: the mount first, the baked-in copy last."""
    out = []
    for d in (TEMPLATES_DIR, TEMPLATES_FALLBACK):
        if d and os.path.isdir(d) and d not in out:
            out.append(d)
    return out


def template_stems(d):
    """Map of template stem -> extension in one directory; .yml beats .tpl."""
    out = {}
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for fn in names:
        for ext in TPL_EXTS:
            if fn.endswith(ext):
                stem = fn[: -len(ext)]
                if stem not in out or ext == ".yml":
                    out[stem] = ext
                break
    return out


def find_template_file(name):
    """Find <name>.yml (or legacy <name>.tpl): mount first, then baked-in; reject traversal."""
    for d in template_dirs():
        root = os.path.realpath(d)
        for ext in TPL_EXTS:
            real = os.path.realpath(os.path.join(d, name + ext))
            if not real.startswith(root + os.sep):
                raise Bad("template path escapes its directory")
            if os.path.isfile(real):
                return real
    return None


def load_template(name):
    if not NAME_RE.match(name or ""):
        raise Bad("invalid template name")
    real = find_template_file(name)
    if not real:
        raise Bad("no such template: " + name)
    try:
        with open(real, "r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise Bad("template %s is not valid YAML: %s" % (name, exc))
    except OSError as exc:
        raise Bad("cannot read template %s: %s" % (name, exc))
    if not isinstance(doc, dict):
        raise Bad("template must be a YAML mapping: " + name)
    tpl = {}
    for key, val in doc.items():
        if key not in TPL_KEYS:
            raise Bad("template has an unknown field: " + str(key))
        if isinstance(val, bool) or not isinstance(val, (str, int, float)):
            raise Bad("template field must be a scalar: " + str(key))
        tpl[str(key)] = str(val).strip()
    for req in ("image",):
        if not tpl.get(req):
            raise Bad("template is missing a required field: " + req)
    if not NAME_RE.match(tpl.get("name", name)):
        raise Bad("invalid name inside template")
    img = tpl["image"]
    if not ALLOW_TAGS and not DIGEST_RE.search(img):
        raise Bad("template image must pin @sha256: digest (set DCA_ALLOW_TAGS=1 to relax)")
    tpl.setdefault("workdir", "/workspace")
    tpl.setdefault("network", DEFAULT_NETWORK)
    if tpl["network"] not in ("", "default") and not NET_RE.match(tpl["network"]):
        raise Bad("invalid template network: " + tpl["network"])
    tpl.setdefault("cpus", "2")
    tpl.setdefault("memory", "4g")
    tpl.setdefault("pids", "512")
    tpl.setdefault("user", "1000:1000")
    if not tpl["workdir"].startswith("/"):
        raise Bad("template workdir must be an absolute path")
    tpl["_file"] = os.path.basename(real)
    return tpl


def list_templates():
    out = []
    seen = set()
    for d in template_dirs():
        for stem in sorted(template_stems(d)):
            if stem in seen:
                continue
            seen.add(stem)
            try:
                out.append(load_template(stem))
            except Bad as exc:
                out.append({"name": stem, "error": str(exc)})
    return out


# ---------------------------------------------------------------- docker

def docker(args, timeout=120, check=True):
    cmd = [DOCKER] + args
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise Bad("docker failed: %s" % (proc.stderr.strip() or proc.stdout.strip()))
    return proc


def cname(task):
    return "devbox-" + task


def require_task(task):
    if not NAME_RE.match(task or ""):
        raise Bad("invalid task name")
    return task


def work_path(task, sub=""):
    base = os.path.realpath(WORK_ROOT)
    path = os.path.realpath(os.path.join(base, task, sub))
    if not (path == os.path.join(base, task) or path.startswith(os.path.join(base, task) + os.sep)):
        raise Bad("path escapes the task workspace")
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
        raise Bad("no container owned by this owner for task: " + task)
    return cname(task)


# ------------------------------------------------------------- actions

def do_create(payload):
    task = require_task(payload.get("task", ""))
    tpl = load_template(payload.get("template", ""))
    work = work_path(task)
    try:
        os.makedirs(work, exist_ok=True)
    except OSError as exc:
        raise Bad(
            "cannot create workspace %s (controller uid=%d): %s. "
            "Fix on the host: sudo mkdir -p %s && sudo chown %d:%d %s "
            "(or run ./setup.sh)."
            % (work, os.getuid(), exc, WORK_ROOT, os.getuid(), os.getgid(), WORK_ROOT)
        )
    os.chmod(work, 0o777)  # devbox runs as a non-root uid; open just this task dir
    existing = docker(["ps", "-a", "--filter", "name=^%s$" % cname(task),
                       "--format", "{{.Names}}"], check=False).stdout.strip()
    if existing:
        raise Bad("task already exists: " + task)
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
        raise Bad("cmd must be a non-empty array of strings")
    if len(cmd) > 64:
        raise Bad("cmd is too long")
    args = ["exec"]
    wd = payload.get("workdir")
    if wd:
        if not wd.startswith("/"):
            raise Bad("workdir must be an absolute path")
        args += ["-w", wd]
    args += ["-e", "TERM=dumb", cname(task)] + cmd
    timeout = min(int(payload.get("timeout", EXEC_TIMEOUT)), EXEC_TIMEOUT)
    try:
        proc = subprocess.run([DOCKER] + args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Bad("exec timed out after %ss" % timeout)
    touch(task)
    return {"rc": proc.returncode,
            "stdout": proc.stdout[-200000:],
            "stderr": proc.stderr[-50000:]}


def check_url(url):
    if not isinstance(url, str) or len(url) > 2000:
        raise Bad("invalid url")
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "ssh"):
        raise Bad("only https:// and ssh:// are allowed (git:// and file:// are refused)")
    host = parsed.hostname or ""
    if not host:
        raise Bad("url has no host")
    if parsed.scheme == "https" and not ALLOW_PRIVATE_URL:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None and (ip.is_private or ip.is_loopback or ip.is_link_local
                               or ip.is_reserved or ip.is_multicast):
            raise Bad("private, loopback and link-local hosts are refused")
        if host.lower() in ("localhost", "localhost.localdomain"):
            raise Bad("private, loopback and link-local hosts are refused")
    return url


def relax_perms(root):
    """Open new files/dirs to a+rwX so a devbox running as another uid can still write."""
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


GIT_HELPER = (
    "!f() { echo username=\\${DCA_GIT_USER:-x-access-token}; "
    "echo password=\\${DCA_GIT_TOKEN}; }; f"
)


def git_env():
    """Environment for git: no prompting; token exposed to the helper via env only."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/bin/true"
    if GIT_TOKEN:
        env["DCA_GIT_TOKEN"] = GIT_TOKEN
        env["DCA_GIT_USER"] = GIT_USER
    return env


def git_args(*args):
    """Prefix git arguments with credential config when a token is configured."""
    if not GIT_TOKEN:
        return ["git"] + list(args)
    return ["git", "-c", "credential.helper=",
            "-c", "credential.helper=" + GIT_HELPER] + list(args)


def do_clone(task, payload):
    require_container(task)
    url = check_url(payload.get("url"))
    sub = payload.get("dir", "")
    if sub and not NAME_RE.match(sub):
        raise Bad("invalid dir")
    dest = work_path(task, sub or os.path.basename(urlparse(url).path).removesuffix(".git") or "repo")
    if os.path.exists(dest) and os.listdir(dest):
        raise Bad("target directory is not empty: " + dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    proc = subprocess.run(git_args("clone", "--", url, dest), capture_output=True,
                          text=True, timeout=600, env=git_env())
    touch(task)
    if proc.returncode != 0:
        raise Bad("git clone failed: " + (proc.stderr.strip()[-2000:] or "unknown"))
    relax_perms(dest)
    return {"path": dest, "rel": os.path.relpath(dest, work_path(task))}


def do_push(task, payload):
    require_container(task)
    sub = payload.get("dir", "")
    dest = work_path(task, sub) if sub else work_path(task)
    if not os.path.isdir(os.path.join(dest, ".git")):
        raise Bad("not a git repository: " + dest)
    proc = subprocess.run(git_args("-C", dest, "push"), capture_output=True, text=True,
                          timeout=600, env=git_env())
    touch(task)
    if proc.returncode != 0:
        raise Bad("git push failed: " + (proc.stderr.strip()[-2000:] or "unknown"))
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


# -------------------------------------------------------------- reaper

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
            raise Bad("request body is too large")
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            raise Bad("request body is not valid JSON")
        if not isinstance(obj, dict):
            raise Bad("request body must be a JSON object")
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
                    raise Bad("unsupported path")
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
                    raise Bad("unknown action: " + action)
            elif method == "DELETE" and path.startswith("/v1/tasks/"):
                parts = path.split("/")
                if len(parts) != 4:
                    raise Bad("unsupported path")
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
        print("DCA_TOKEN is missing or shorter than 16 characters; refusing to start", file=sys.stderr)
        return 2
    if not shutil.which(DOCKER):
        print("docker CLI not found on PATH", file=sys.stderr)
        return 2
    try:
        os.makedirs(WORK_ROOT, exist_ok=True)
        probe = os.path.join(WORK_ROOT, ".write-probe")
        with open(probe, "w") as fh:
            fh.write("ok")
        os.unlink(probe)
    except OSError as exc:
        print(
            "WORK_ROOT is not writable: %s\n"
            "  controller runs as uid=%d gid=%d inside its container.\n"
            "  The host directory must be owned by (or writable by) that uid;\n"
            "  a container cannot chown the host side itself.\n"
            "\n"
            "  On the host, either run the bundled setup (recommended, idempotent):\n"
            "      ./setup.sh\n"
            "  or do it by hand:\n"
            "      sudo mkdir -p %s && sudo chown %d:%d %s\n"
            "  or point DCA_HOST_WORK_ROOT at a directory that uid can already write,\n"
            "  e.g. $PWD/work inside your clone of this repo.\n"
            "\n"
            "  underlying error: %s"
            % (WORK_ROOT, os.getuid(), os.getgid(),
               WORK_ROOT, os.getuid(), os.getgid(), WORK_ROOT, exc),
            file=sys.stderr,
        )
        return 2
    threading.Thread(target=reaper, daemon=True).start()
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    print("devbox controller listening on %s:%s (owner=%s, work_root=%s, network=%s)"
          % (BIND, PORT, OWNER, WORK_ROOT, DEFAULT_NETWORK or "(docker default)"), flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
