#!/usr/bin/env python3
"""Resolve a docker image reference to its multi-arch digest and report platforms.

No docker daemon needed: talks to the registry HTTP API (Docker Hub, ghcr, mcr).
Used to pin templates/*.yml to @sha256 digests, and to check the image really
carries the architectures the devbox host needs (linux/arm64 for a Pi).

  python3 tools/pin-images.py golang:1.23-alpine node:22-alpine
  python3 tools/pin-images.py --json refs.txt
"""
import json
import sys
import urllib.request
import urllib.error

ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])

REGISTRIES = {
    # host: (api base, token url builder, repo prefix)
    "docker.io": ("https://registry-1.docker.io",
                  "https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull",
                  "library/"),
    "ghcr.io": ("https://ghcr.io", None, ""),
    "mcr.microsoft.com": ("https://mcr.microsoft.com", None, ""),
}


def split_ref(ref):
    host = "docker.io"
    body = ref
    if "/" in body.split(":")[0] or body.split("/")[0].count(".") or body.split("/")[0] == "localhost":
        head = body.split("/")[0]
        if head in REGISTRIES:
            host = head
            body = body[len(head) + 1:]
    name, _, tag = body.rpartition(":")
    if not name:
        name, tag = body, "latest"
    if "@" in tag:
        tag = "latest"
    api, tok, prefix = REGISTRIES[host]
    repo = name if "/" in name else prefix + name
    return host, api, tok, repo, tag


def hget(headers, name):
    """Case-insensitive header lookup (error responses keep the server's casing)."""
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return "" or None


def http(url, headers, method="GET"):
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read() or b""


def token_for(tok_url, repo):
    if not tok_url:
        # ghcr/mcr accept anonymous pulls for public images
        return None
    status, _, body = http(tok_url.format(repo=repo), {"Accept": "application/json"})
    if status != 200:
        raise RuntimeError("token %s -> %s" % (tok_url, status))
    return json.loads(body)["token"]


def resolve(ref):
    host, api, tok_url, repo, tag = split_ref(ref)
    out = {"ref": ref, "repo": repo, "tag": tag, "registry": host}
    token = token_for(tok_url, repo)
    headers = {"Accept": ACCEPT}
    if token:
        headers["Authorization"] = "Bearer " + token
    url = "%s/v2/%s/manifests/%s" % (api, repo, tag)
    status, hdrs, body = http(url, headers, method="GET")
    if status == 401 and not token:
        challenge = hget(hdrs, "WWW-Authenticate") or ""
        if "realm=" in challenge:
            realm = challenge.split('realm="')[1].split('"')[0]
            sep = "&" if "?" in realm else "?"
            scope = ""
            if 'scope="' in challenge:
                scope = challenge.split('scope="')[1].split('"')[0]
            url_token = "%s%sservice=%s%s" % (realm, sep, host, ("&scope=" + scope) if scope else "")
            status, _, tbody = http(url_token, {"Accept": "application/json"})
            if status == 200:
                token = json.loads(tbody).get("token") or json.loads(tbody).get("access_token")
                headers["Authorization"] = "Bearer " + token
                status, hdrs, body = http(url, headers, method="GET")
    out["http"] = status
    if status != 200:
        out["error"] = "registry %s: %s" % (status, body[:200].decode("utf-8", "replace"))
        return out
    digest = hget(hdrs, "Docker-Content-Digest")
    out["digest"] = digest
    try:
        doc = json.loads(body)
    except ValueError:
        out["error"] = "unparsable manifest"
        return out
    if "manifests" in doc:
        combos = []
        for m in doc["manifests"]:
            p = m.get("platform") or {}
            if p.get("os") == "unknown":
                continue
            combos.append("%s/%s" % (p.get("os"), p.get("architecture")))
        out["platforms"] = sorted(set(combos))
        out["arm64"] = "linux/arm64" in out["platforms"]
        out["amd64"] = "linux/amd64" in out["platforms"]
    else:
        # single-arch manifest: the config blob names os/architecture
        cfg = (doc.get("config") or {}).get("digest")
        blob_url = "%s/v2/%s/blobs/%s" % (api, repo, cfg)
        status, _, cbody = http(blob_url, headers)
        if status != 200:
            out["platforms"] = ["single-manifest"]
            out["arm64"] = None
            out["amd64"] = None
            return out
        cdoc = json.loads(cbody)
        arch = cdoc.get("architecture")
        osname = cdoc.get("os")
        out["platforms"] = ["%s/%s" % (osname, arch)]
        out["arm64"] = arch == "arm64"
        out["amd64"] = arch == "amd64"
    return out


def main(argv):
    as_json = "--json" in argv
    refs = [a for a in argv if not a.startswith("--")]
    if not refs:
        print(__doc__)
        return 2
    results = [resolve(r) for r in refs]
    if as_json:
        print(json.dumps(results, indent=2))
        return 0
    for r in results:
        if r.get("digest"):
            print("%-42s %s  arm64=%s amd64=%s" % (
                r["ref"], r["digest"], r.get("arm64"), r.get("amd64")))
        else:
            print("%-42s FAILED: %s" % (r["ref"], r.get("error")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
