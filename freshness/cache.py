"""
Verdict cache: sqlite in <repo>/.staleguard/cache.sqlite3 (self-gitignored).

Key = (distribution, version, symbol). The same API at the same version gets
the same answer, so a repeated check costs no Tavily credit and no judge
call. Entries expire after FRESHNESS_CACHE_TTL_DAYS (docs change).
Only settled verdicts (changed / supported, which always carry a verified
quote) are cached; `unverifiable` is asked again next time.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent import lookups
from agent.lookups import FreshnessStatus
from agent.session import state_dir
from freshness.judge import Verdict


class VerdictCache:
    _SCHEMA = """CREATE TABLE IF NOT EXISTS verdicts (
        distribution TEXT NOT NULL, version TEXT NOT NULL, symbol TEXT NOT NULL,
        verdict TEXT NOT NULL, created TEXT NOT NULL,
        PRIMARY KEY (distribution, version, symbol))"""

    def __init__(self, path: Path, *, ttl_days: int = lookups.FRESHNESS_CACHE_TTL_DAYS) -> None:
        self.path, self.ttl = path, timedelta(days=ttl_days)
        with sqlite3.connect(self.path) as db:
            db.execute(self._SCHEMA)

    @classmethod
    def for_repo(cls, repo_root: Path) -> "VerdictCache":
        return cls(state_dir(repo_root) / lookups.CACHE_DB_FILENAME)

    def get(self, distribution: str, version: str, symbol: str) -> Verdict | None:
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT verdict, created FROM verdicts WHERE distribution = ? "
                             "AND version = ? AND symbol = ?",
                             (distribution, version, symbol)).fetchone()
        if row is None or datetime.now(UTC) - datetime.fromisoformat(row[1]) > self.ttl:
            return None
        return Verdict.model_validate_json(row[0])

    def put(self, distribution: str, version: str, verdict: Verdict) -> None:
        if verdict.status is FreshnessStatus.UNVERIFIABLE:
            return  # not settled; ask again next time
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT OR REPLACE INTO verdicts VALUES (?, ?, ?, ?, ?)",
                       (distribution, version, verdict.symbol, verdict.model_dump_json(),
                        datetime.now(UTC).isoformat()))
