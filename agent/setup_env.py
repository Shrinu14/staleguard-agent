"""
`staleguard init`: create .env for a fresh clone.

  - copies .env.example to .env if .env does not exist
  - fills the gateway secrets that are EMPTY (GENERATED_ENV_SECRETS) with random
    values; values already set are never touched, so it is safe to re-run
  - never prints a secret; returns only the names it filled

After this the only value a user must add is TAVILY_API_KEY. The file is
written with owner-only permissions (0600).
"""

import re
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from agent import lookups


@dataclass(slots=True)
class InitResult:
    created: bool = False
    generated: list[str] = field(default_factory=list)
    missing_tavily: bool = True


def _empty_line(name: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(name)}=[ \t]*$", re.MULTILINE)


def ensure_env(root: Path) -> InitResult:
    env = root / lookups.ENV_FILE
    result = InitResult()
    if not env.exists():
        example = root / lookups.ENV_EXAMPLE_FILE
        if not example.exists():
            raise FileNotFoundError(f"{example} not found")
        shutil.copyfile(example, env)
        result.created = True
    text = env.read_text(encoding="utf-8")
    for name, (prefix, nbytes) in lookups.GENERATED_ENV_SECRETS.items():
        pattern = _empty_line(name)
        if pattern.search(text):
            value = prefix + secrets.token_hex(nbytes)
            text = pattern.sub(f"{name}={value}", text, count=1)
            result.generated.append(name)
        elif not re.search(rf"^{re.escape(name)}=", text, re.MULTILINE):
            text = text.rstrip("\n") + f"\n{name}={prefix + secrets.token_hex(nbytes)}\n"
            result.generated.append(name)
    env.write_text(text, encoding="utf-8")
    env.chmod(0o600)
    result.missing_tavily = bool(_empty_line("TAVILY_API_KEY").search(text)) or \
        not re.search(r"^TAVILY_API_KEY=", text, re.MULTILINE)
    return result
