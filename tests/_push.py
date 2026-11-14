#!/usr/bin/env python3
"""Push origin/main using the saved GitHub token, without printing it."""
import os
import subprocess
import sys

tok = os.environ.get("GITHUB_SYNAPSE") or os.environ.get("GITHUB_TOKEN")
if not tok:
    sys.exit("no token in env")
url = "https://x-access-token:" + tok + "@github.com/sysnapse/devbox-controller.git"
p = subprocess.run(["git", "push", url, "main"], capture_output=True, text=True)
out = (p.stdout + p.stderr).replace(tok, "***")
print("rc", p.returncode)
print(out[-1500:])
sys.exit(p.returncode)
