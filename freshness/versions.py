"""
Step 2 of the freshness pipeline: which version of a library does the REPO
use? (Not the agent's own environment — that could be a different version.)
No LLM. Sources, best first:

  1. <repo>/.venv site-packages   installed version (exact)
  2. <repo>/uv.lock                locked version (exact)
  3. <repo>/requirements*.txt      a pin or range, e.g. "==0.28.1" or ">=2"
  4. <repo>/pyproject.toml         [project].dependencies, a range
  5. unknown                       the judge then compares with the latest docs

Import names and PyPI names can differ (`import yaml` -> PyYAML), so names go
through lookups.IMPORT_TO_DISTRIBUTION first, and are compared in PEP 503
normalized form ("Py_YAML" == "pyyaml").
"""

import importlib.metadata
import tomllib
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from agent import lookups


@dataclass(frozen=True, slots=True)
class VersionInfo:
    distribution: str
    version: str | None   # "0.28.1", ">=2.0", or None if unknown
    source: str           # where it came from, shown in the report

    @property
    def label(self) -> str:
        return self.version or "unknown"


def distribution_for(import_name: str) -> str:
    """'yaml' -> 'PyYAML'; 'google.generativeai' -> 'google-generativeai'."""
    parts = import_name.split(".")
    for i in range(len(parts), 0, -1):
        hit = lookups.IMPORT_TO_DISTRIBUTION.get(".".join(parts[:i]))
        if hit:
            return hit
    return parts[0]


def find_version(repo_root: Path, import_name: str) -> VersionInfo:
    dist = distribution_for(import_name)
    key = canonicalize_name(dist)
    for finder in (_from_venv, _from_uv_lock, _from_requirements, _from_pyproject):
        found = finder(repo_root, key)
        if found is not None:
            version, source = found
            return VersionInfo(dist, version, source)
    return VersionInfo(dist, None, "not found in repo")


# ── sources ──────────────────────────────────────────────────
def _from_venv(root: Path, key: str) -> tuple[str, str] | None:
    for site in sorted((root / ".venv").glob("lib/python*/site-packages")):
        for d in importlib.metadata.distributions(path=[str(site)]):
            if canonicalize_name(d.metadata["Name"] or "") == key:
                return d.version, ".venv (installed)"
    return None


def _from_uv_lock(root: Path, key: str) -> tuple[str, str] | None:
    data = _toml(root / "uv.lock")
    for pkg in data.get("package", []):
        if canonicalize_name(str(pkg.get("name", ""))) == key and pkg.get("version"):
            return str(pkg["version"]), "uv.lock"
    return None


def _from_requirements(root: Path, key: str) -> tuple[str, str] | None:
    for path in sorted(root.glob("requirements*.txt")):
        try:
            lines = path.read_text(encoding=lookups.FILE_ENCODING).splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            hit = _match(line.split("#", 1)[0].strip(), key)
            if hit is not None:
                return hit, path.name
    return None


def _from_pyproject(root: Path, key: str) -> tuple[str, str] | None:
    deps = _toml(root / "pyproject.toml").get("project", {}).get("dependencies", [])
    for dep in deps:
        hit = _match(str(dep), key)
        if hit is not None:
            return hit, "pyproject.toml"
    return None


def _match(spec: str, key: str) -> str | None:
    """'openai==0.28.1' -> '==0.28.1' when it names `key`; '' -> None."""
    if not spec or spec.startswith("-"):
        return None
    try:
        req = Requirement(spec)
    except InvalidRequirement:
        return None
    if canonicalize_name(req.name) != key:
        return None
    text = str(req.specifier)
    if text.startswith("==") and "," not in text:
        return text[2:]  # an exact pin is a version, not a range
    return text or "any"


def _toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding=lookups.FILE_ENCODING))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}
