"""Exit 0 iff LocalStack's s3 and secretsmanager services are ready — used
by scripts/local-setup.sh's wait-for-healthy loop.

Deliberately not a `grep` on a literal status string: LocalStack transitions
a service through more than one healthy-ish state over its lifetime (seen
empirically: "available" right after startup, "running" once a service has
actually been used) — a status of "disabled" is the only state that means
"not usable". Checking for the presence of a positive keyword is more
robust than trying to enumerate every non-"disabled" string in advance.
"""

import sys
import urllib.request

with urllib.request.urlopen("http://localhost:4566/_localstack/health", timeout=5) as resp:
    import json

    services = json.load(resp)["services"]

for name in ("s3", "secretsmanager"):
    if services.get(name) in (None, "disabled"):
        sys.exit(1)
