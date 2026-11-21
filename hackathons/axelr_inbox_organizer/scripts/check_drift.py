"""Fail if submission/SKILL.md and axelr/__init__.py SKILL_MANIFEST disagree.

The Markdown Skill and the Python engine must describe the same skill:
same name, same version, same track. This script is the enforceable claim
that the two halves are one.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = ROOT / "submission" / "SKILL.md"
ENGINE = ROOT / "axelr" / "__init__.py"


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        raise SystemExit(f"error: {SKILL_MD} missing YAML frontmatter")
    end = text.find("\n---", 3)
    if end == -1:
        raise SystemExit(f"error: {SKILL_MD} frontmatter not closed")
    fields: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip(">").strip()
    return fields


def parse_manifest() -> dict[str, str]:
    text = ENGINE.read_text(encoding="utf-8")
    fields: dict[str, str] = {}
    for key in ("id", "name", "version", "track"):
        m = re.search(rf'"{key}"\s*:\s*"([^"]+)"', text)
        if not m:
            raise SystemExit(f"error: SKILL_MANIFEST missing {key!r}")
        fields[key] = m.group(1)
    return fields


def main() -> int:
    fm = parse_frontmatter(SKILL_MD.read_text(encoding="utf-8"))
    mf = parse_manifest()
    problems: list[str] = []
    if fm.get("name") != mf["id"]:
        problems.append(f"name: SKILL.md={fm.get('name')!r} manifest={mf['id']!r}")
    if fm.get("version") != mf["version"]:
        problems.append(f"version: SKILL.md={fm.get('version')!r} manifest={mf['version']!r}")
    if fm.get("classification") != mf["track"]:
        problems.append(f"classification: SKILL.md={fm.get('classification')!r} manifest={mf['track']!r}")
    if problems:
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print("✓ submission/SKILL.md and axelr/__init__.py agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
