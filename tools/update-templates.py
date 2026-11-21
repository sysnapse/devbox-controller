#!/usr/bin/env python3
"""Render templates/*.yml from one table, pinning every image to its @sha256 digest.

The table below is the single place to add a language. Each entry is resolved
against the registry, checked for a linux/arm64 manifest (a Pi 5 host is arm64;
an image without it would fail at `dc new`, not at render time), then written as
templates/<name>.yml.

  python3 tools/update-templates.py            # resolve, verify, write
  python3 tools/update-templates.py --check     # resolve and verify, write nothing
  python3 tools/update-templates.py --only rust # refresh a single template

Notes on the choices:
  * alpine variants first: smaller layers on a host whose docker data-root may sit
    on an SD card.
  * images without an arm64 variant fall back to their Debian/Ubuntu variant, or
    are dropped from the table with the reason recorded in README.md.
  * packages are installed with whatever manager the image ships (apk/apt-get/dnf);
    Debian images get the apt spelling of the same tool.
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TEMPLATES = os.path.join(ROOT, "templates")


def load_pin_module():
    path = os.path.join(HERE, "pin-images.py")
    spec = importlib.util.spec_from_file_location("pin_images", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# name, image:tag, cpus, memory, pids, packages, notes
TABLE = [
    ("base", "alpine:3.21", 1, "1g", 256, ["git"],
     "minimal Alpine toolchain; use a language template for real work"),
    ("c", "alpine:3.21", 2, "2g", 512, ["build-base", "git", "cmake", "cmocka-dev", ffmpeg],
     "C toolchain: gcc, make, cmake, cmocka"),
    ("cpp", "alpine:3.21", 2, "2g", 512, ["build-base", "git", "cmake", ffmpeg],
     "C++ toolchain: g++, make, cmake"),
    ("fortran", "gcc:15", 2, "2g", 512, ["git"],
     "GFortran from the official GCC image (Debian base, gfortran already present)"),
    ("lua", "alpine:3.21", 1, "1g", 256, ["lua5.4", "git"],
     "Lua 5.4; luarocks is not packaged for Alpine 3.21, install it with the agent if needed"),
    ("go", "golang:1.27-alpine", 2, "4g", 512, ["git", "make"],
     "Go toolchain; the image already carries git"),
    ("rust", "rust:1.100-alpine", 2, "4g", 512, ["git", "build-base", ffmpeg],
     "rustc and cargo; build-base is needed to link musl targets"),
    ("node22", "node:22-alpine", 2, "4g", 512, ["git"],
     "Node.js 22 LTS with npm; git is already in the image"),
    ("node24", "node:24-alpine", 2, "4g", 512, ["git"],
     "Node.js 24 with npm"),
    ("deno", "denoland/deno:alpine", 2, "2g", 512, ["git"],
     "Deno runtime (TypeScript out of the box)"),
    ("bun", "oven/bun:1-alpine", 2, "2g", 512, ["git"],
     "Bun runtime and package manager"),
    ("python311", "python:3.11-alpine", 2, "4g", 512, ["git", "build-base", ffmpeg],
     "Python 3.11 with pip; build-base for C extensions"),
    ("python312", "python:3.12-alpine", 2, "4g", 512, ["git", "build-base", ffmpeg],
     "Python 3.12 with pip; build-base for C extensions"),
    ("python313", "python:3.13-alpine", 2, "4g", 512, ["git", "build-base", ffmpeg],
     "Python 3.13 with pip; build-base for C extensions"),
    ("ruby", "ruby:3.4-alpine", 2, "4g", 512, ["git", "build-base", "yaml-dev", ffmpeg],
     "Ruby 3.4 with bundler; build-base for native gems"),
    ("php", "php:8.5-alpine", 2, "4g", 512, ["git"],
     "PHP 8.5 CLI; composer is not an Alpine package, fetch it with the agent"),
    ("perl", "perl:5.42-slim", 2, "2g", 512, ["git", "build-essential"],
     "Perl 5.42 with cpan; build-essential for XS modules"),
    ("java17", "eclipse-temurin:17-jdk", 2, "4g", 512, ["git"],
     "JDK 17 with javac; Debian base because 17-jdk-alpine has no arm64 build"),
    ("java21", "eclipse-temurin:21-jdk-alpine", 2, "4g", 512, ["git"],
     "JDK 21 with javac"),
    ("java25", "eclipse-temurin:25-jdk-alpine", 2, "4g", 512, ["git"],
     "JDK 25 with javac"),
    ("kotlin", "eclipse-temurin:21-jdk-alpine", 2, "4g", 512, ["git"],
     "JDK 21 base; no arm64 official Kotlin image exists, so install kotlinc inside"),
    ("clojure", "clojure:temurin-25-tools-deps-bookworm-slim", 2, "4g", 512, ["git"],
     "Clojure with the Clojure CLI and tools.deps"),
    ("groovy", "groovy:jdk21-noble", 2, "4g", 512, ["git"],
     "Groovy with the JDK 21 image underneath"),
    ("elixir", "elixir:1.20-alpine", 2, "4g", 512, ["git", "build-base", ffmpeg],
     "Elixir with mix and iex; build-base for NIFs"),
    ("erlang", "erlang:28-alpine", 2, "4g", 512, ["git", "build-base", ffmpeg],
     "Erlang/OTP 28 with rebar3"),
    ("haskell", "haskell:9.12", 2, "6g", 512, ["git"],
     "GHC 9.12 with cabal and stack; heavy image, pull only when needed"),
    ("ocaml", "ocaml/opam:debian-12-ocaml-5.3", 2, "4g", 512, ["git"],
     "OCaml 5.3 with opam and dune"),
    ("crystal", "crystallang/crystal:1.22.3-alpine", 2, "4g", 512, ["git", "build-base", ffmpeg],
     "Crystal 1.22 with shards"),
    ("nim", "nimlang/nim:2.2.0", 2, "2g", 512, ["git"],
     "Nim 2.2 with nimble (2.4.0 is amd64-only, so 2.2.0 is pinned)"),
    ("julia", "julia:1.12", 2, "6g", 512, ["git"],
     "Julia 1.12 with the package manager; heavy image"),
    ("r", "r-base:4.5.2", 2, "4g", 512, ["git"],
     "R 4.5 with install.packages support"),
    ("swift", "swift:6.2", 2, "6g", 512, ["git"],
     "Swift 6.2 toolchain (Debian base); heavy image"),
    ("dart", "dart:stable", 2, "4g", 512, ["git"],
     "Dart SDK with pub"),
    ("dotnet", "mcr.microsoft.com/dotnet/sdk:10.0", 2, "6g", 512, ["git"],
     "dotnet SDK 10 (C#, F#, VB); Debian base from mcr.microsoft.com"),
]

HEADER = """# {name} - {notes}
# image pins an @sha256 digest; tags are rejected by the controller
name: {name}
image: {image}
workdir: /workspace
network: default
cpus: {cpus}
memory: {memory}
pids: {pids}
user: "1000:1000"
# Installed at create time with whatever manager the image ships (apk/apt-get/dnf)
packages: [{packages}]
notes: "{notes}"
"""


def render(name, image_ref, cpus, memory, pids, packages, notes):
    return HEADER.format(name=name, image=image_ref, cpus=cpus, memory=memory,
                         pids=pids, packages=", ".join(packages), notes=notes)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="resolve and verify only")
    ap.add_argument("--only", default="", help="single template name")
    ap.add_argument("--json", default="", help="write the resolution report here")
    args = ap.parse_args()

    pin = load_pin_module()
    rows = [r for r in TABLE if not args.only or r[0] == args.only]
    if not rows:
        print("no such template in the table: " + args.only)
        return 2
    os.makedirs(TEMPLATES, exist_ok=True)
    report = []
    failed = []
    for (name, ref, cpus, memory, pids, packages, notes) in rows:
        res = pin.resolve(ref)
        row = {"name": name, "ref": ref, "digest": res.get("digest"),
               "arm64": res.get("arm64"), "platforms": res.get("platforms"),
               "error": res.get("error")}
        report.append(row)
        if not res.get("digest"):
            failed.append((name, res.get("error")))
            print("FAIL resolve %-10s %s: %s" % (name, ref, res.get("error")))
            continue
        if res.get("arm64") is False:
            failed.append((name, "no linux/arm64 manifest"))
            print("FAIL arm64  %-10s %s" % (name, ref))
            continue
        pinned = ref.split("@")[0] + "@" + res["digest"]
        text = render(name, pinned, cpus, memory, pids, packages, notes)
        path = os.path.join(TEMPLATES, name + ".yml")
        if args.check:
            print("ok   %-10s %s arm64=%s" % (name, res["digest"], res.get("arm64")))
            continue
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
        print("wrote %-10s %s arm64=%s" % (path, res["digest"][:19] + "...",
                                           res.get("arm64")))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    if failed:
        print("\n%d template(s) refused:" % len(failed))
        for name, why in failed:
            print("  %-10s %s" % (name, why))
        return 1
    print("\nall %d templates resolved with linux/arm64" % len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
