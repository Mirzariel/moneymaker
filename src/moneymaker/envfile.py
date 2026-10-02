"""Read/update the local `.env` file written by the setup page."""
from __future__ import annotations

import os
import re
import secrets as pysecrets
import shutil
from pathlib import Path

_KEY = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def update_env(path: Path, updates: dict[str, str]) -> None:
    """Set KEY=value lines in place (keeping comments/order), append missing keys. Owner-only permissions."""
    for k, v in updates.items():
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", k):
            raise ValueError(f"invalid env key {k!r}")
        if any(c in v for c in "\r\n\0"):
            raise ValueError(f"{k}: value must be a single line")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out = []
    for line in lines:
        m = _KEY.match(line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    out.extend(f"{k}={v}" for k, v in remaining.items())
    content = "\n".join(out) + "\n"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    if os.name == "posix":
        os.chmod(tmp, 0o600)
    try:
        tmp.replace(path)
    except OSError:  # e.g. a single-file Docker bind mount can't be replaced atomically
        tmp.unlink(missing_ok=True)
        path.write_text(content, encoding="utf-8")


def bootstrap(root: Path) -> list[str]:
    """First run on a fresh clone: create .env / config.yaml and an API token. Returns what was created."""
    created = []
    env = root / ".env"
    if not env.exists():
        example = root / ".env.example"
        if example.exists():
            shutil.copyfile(example, env)
        else:
            env.touch()
        created.append(".env")
    text = env.read_text(encoding="utf-8")
    m = re.search(r"^API_TOKEN=(.*)$", text, re.M)
    if not m or len(m.group(1).strip()) < 16:
        update_env(env, {"API_TOKEN": pysecrets.token_urlsafe(32)})
        created.append("API_TOKEN")
    elif os.name == "posix":
        os.chmod(env, 0o600)
    cfg = root / "config.yaml"
    if not cfg.exists() and (root / "config.example.yaml").exists():
        shutil.copyfile(root / "config.example.yaml", cfg)
        created.append("config.yaml")
    return created
