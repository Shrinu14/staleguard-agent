"""Tests for `staleguard init` (agent/setup_env.py). Offline, tmp dirs only."""

import stat
from pathlib import Path

from agent import lookups
from agent.setup_env import ensure_env

ROOT = Path(__file__).resolve().parent.parent


def _values(env: Path) -> dict[str, str]:
    out = {}
    for line in env.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def test_fresh_clone_gets_env_with_generated_keys(tmp_path: Path) -> None:
    (tmp_path / lookups.ENV_EXAMPLE_FILE).write_text(
        (ROOT / lookups.ENV_EXAMPLE_FILE).read_text())
    res = ensure_env(tmp_path)
    env = tmp_path / lookups.ENV_FILE
    vals = _values(env)
    assert res.created and res.missing_tavily
    assert sorted(res.generated) == sorted(lookups.GENERATED_ENV_SECRETS)
    assert vals["BIFROST_VK_DEFAULT"].startswith("sk-bf-")
    assert vals["BIFROST_VK_DEFAULT"] != vals["BIFROST_VK_LOCAL"]
    assert len(vals["BIFROST_SETUP_TOKEN"]) == 64
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    # comments and other values survive
    assert vals["MODEL_LOCAL"] == "ollama/qwen2.5-coder:7b"
    assert "NEVER commit real keys" in env.read_text()


def test_rerun_never_overwrites(tmp_path: Path) -> None:
    env = tmp_path / lookups.ENV_FILE
    env.write_text("BIFROST_VK_DEFAULT=sk-bf-mine\nBIFROST_VK_LOCAL=\nTAVILY_API_KEY=tvly-x\n")
    res = ensure_env(tmp_path)
    vals = _values(env)
    assert not res.created and not res.missing_tavily
    assert vals["BIFROST_VK_DEFAULT"] == "sk-bf-mine"
    assert vals["BIFROST_VK_LOCAL"].startswith("sk-bf-")
    assert "BIFROST_SETUP_TOKEN" in vals  # missing line is appended
    before = env.read_text()
    assert ensure_env(tmp_path).generated == [] and env.read_text() == before
