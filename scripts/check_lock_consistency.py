"""Verify requirements.lock satisfies the documented ranges in requirements.txt.

The lockfile is curated (advisory pins, exact provider builds) rather than
pip-compile output, so regenerating it would churn unrelated upgrades. This
gate proves the weaker, load-bearing property instead: every ranged
requirement resolves to exactly one locked version inside its specifiers.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _parse_requirements(path: Path) -> dict[str, str]:
    requirements: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        match = re.match(r"^([A-Za-z0-9_.\-]+(?:\[[^\]]*\])?)\s*(.*)$", line)
        if not match:
            continue
        name = re.sub(r"\[.*\]", "", match.group(1)).lower().replace("_", "-")
        requirements[name] = match.group(2).strip()
    return requirements


def _parse_lock(path: Path) -> dict[str, str]:
    locked: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        match = re.match(r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?==([^\s;]+)", line)
        if match:
            locked[match.group(1).lower().replace("_", "-")] = match.group(2)
    return locked


def _satisfies(version: str, specifiers: str) -> bool:
    from packaging.version import Version

    wanted = Version(version)
    for clause in specifiers.split(","):
        clause = clause.strip()
        if not clause:
            continue
        match = re.match(r"^(===|==|~=|!=|>=|<=|>|<)\s*(.+)$", clause)
        if not match:
            continue
        operator, target = match.group(1), Version(match.group(2).split(";")[0].strip())
        if operator == "==" and wanted != target:
            return False
        if operator == ">=" and wanted < target:
            return False
        if operator == "<=" and wanted > target:
            return False
        if operator == ">" and wanted <= target:
            return False
        if operator == "<" and wanted >= target:
            return False
        if operator == "!=" and wanted == target:
            return False
        if operator == "~=" and not (wanted >= target and wanted.release[0] == target.release[0]):
            return False
        if operator == "===" and str(wanted) != str(target):
            return False
    return True


def main() -> int:
    requirements = _parse_requirements(REPO_ROOT / "requirements.txt")
    locked = _parse_lock(REPO_ROOT / "requirements.lock")
    problems: list[str] = []
    for name, specifiers in sorted(requirements.items()):
        if name not in locked:
            problems.append(f"{name}: missing from requirements.lock")
        elif specifiers and not _satisfies(locked[name], specifiers):
            problems.append(f"{name}: locked {locked[name]} violates '{specifiers}'")
    if problems:
        print("Lockfile is inconsistent with requirements.txt:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"Lockfile OK: {len(requirements)} requirements pinned within range.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
