"""
Session log: one JSON object per line (JSONL), one file per run.

Every event the agent loop emits (model replies, tool calls, approvals,
token usage, model switches) is appended here with a timestamp, so a run
can be replayed, audited, or turned into benchmark numbers later.

Files live in <repo>/.staleguard/sessions/, a folder that contains its own
.gitignore ("*"), so logs are never committed by accident.
"""

import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent import lookups


def state_dir(repo_root: Path) -> Path:
    """<repo>/.staleguard, created on first use with a self-ignoring .gitignore."""
    path = repo_root / lookups.STATE_DIRNAME
    path.mkdir(exist_ok=True)
    ignore = path / ".gitignore"
    if not ignore.exists():
        ignore.write_text(lookups.STATE_GITIGNORE_CONTENT, encoding=lookups.FILE_ENCODING)
    return path


class SessionLog:
    def __init__(self, repo_root: Path) -> None:
        folder = state_dir(repo_root) / lookups.SESSION_LOG_DIRNAME
        folder.mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.path = folder / f"{stamp}-{secrets.token_hex(3)}.jsonl"

    def write(self, event: dict[str, Any]) -> None:
        record = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), **event}
        with self.path.open("a", encoding=lookups.FILE_ENCODING) as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
