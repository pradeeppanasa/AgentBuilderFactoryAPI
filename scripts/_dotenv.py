"""Minimal, dependency-free .env reader for the bootstrap scripts.

Deliberately NOT bash `source .env` — .env.example's real-deployment
placeholders (e.g. `panasa-transcripts-<account>`) contain `<`/`>`, which
bash parses as redirection syntax and fails on with a syntax error. Also
deliberately not relying on `app.config.settings` for everything: bootstrap
-only keys like DEFAULT_ADMIN_EMAIL aren't Settings fields, so
pydantic-settings never surfaces them even though it reads the same file.
Not python-dotenv (not a project dependency) — this covers exactly the
`KEY=value  # comment` shape every file in this repo actually uses.
"""

from __future__ import annotations

import re
from pathlib import Path

_COMMENT_RE = re.compile(r"(?:^|\s)#.*$")


def load_env(path: str | Path = ".env") -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = _COMMENT_RE.sub("", raw_line).strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values
