#!/usr/bin/env python3
"""Inspect image configs through the registry (no docker needed).

Reports Entrypoint / Cmd / User / WorkingDir for a list of image references, so a
template author knows whether `sleep infinity` will actually run and whether the
image expects root. Shares the ref parsing with pin-images.py.

  python3 tools/image-config.py bash:5-alpine sqltech/groovy:latest
"""
import json
import sys
import urllib.request

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from importlib import import_module

pin = import_module("pin-images".replace("-", "_")) if False else None  # noqa: keep flake quiet


ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


def hget(headers, name):
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None


def req(url, headers, method="GET"):
    r = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:  # noqa: F821
        return exc.code, dict(exc.headers or {}), exc.read() or b""


def parse(ref):
    host = "docker.io"
    body = ref
    head = body.split("/")[0]
    if "/" in body and ("." in head or ":" in head or head == "localhost"):
        host = head
        body = body[len(head) + 1:]
    name, _, tag = body.rpartition(":")
    if not name:
        name, tag = body, "latest"
    return host, name, tag


def token(host, repo):
    if host == "docker.io":
        url = ("https://auth.docker.io/token?service=registry.docker.io"
               "&scope=repository:%s:pull" % repo)
    elif host == "mcr.microsoft.com":
        return None
    else:
        url = "https://%s/token?scope=repository:%s:pull&service=%s" % (host, repo, host)
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            doc = json.loads(resp.read())
        return doc.get("token") or doc.get("access_token")
    except Exception:
        return None


def config_of(ref):
    host, name, tag = parse(ref)
    api = {"docker.io": "https://registry-1.docker.io", "ghcr.io": "https://ghcr.io",
           "mcr.microsoft.com": "https://mcr.microsoft.com"}.get(host, "https://" + host)
    repo = name if "/" in name or host != "docker.io" else "library/" + name
    tok = token(host, repo)
    hdr = {"Accept": ACCEPT}
    if tok:
        hdr["Authorization"] = "Bearer " + tok
    status, hdrs, body = req("%s/v2/%s/manifests/%s" % (api, repo, tag), hdr)
    if status != 200:
        return {"ref": ref, "error": "manifest %s" % status}
    doc = json.loads(body)
    cfg_digest = None
    if "manifests" in doc:
        for m in doc["manifests"]:
            p = m.get("platform") or {}
            if p.get("os") == "linux" and p.get("architecture") == "arm64":
                cfg_digest = m["digest"]
                break
        if not cfg_digest:
            return {"ref": ref, "error": "no linux/arm64 manifest"}
    else:
        cfg_digest = doc["config"]["digest"]
    status, _, cbody = req("%s/v2/%s/blobs/%s" % (api, repo, cfg_digest), hdr)
    if status != 200:
        return {"ref": ref, "error": "config blob %s" % status}
    cfg = json.loads(cbody)["config"]
    return {"ref": ref, "entrypoint": cfg.get("Entrypoint"), "cmd": cfg.get("Cmd"),
            "user": cfg.get("User") or "", "workdir": cfg.get("WorkingDir") or ""}


if __name__ == "__main__":
    for r in sys.argv[1:]:
        c = config_of(r)
        if "error" in c:
            print("%-44s ERROR %s" % (r, c["error"]))
        else:
            print("%-44s entrypoint=%s cmd=%s user=%r wd=%r" % (
                r, c["entrypoint"], c["cmd"], c["user"], c["workdir"]))
