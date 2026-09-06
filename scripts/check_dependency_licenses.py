#!/usr/bin/env python3
"""Fail on dependency licenses unsuitable for the distributed application."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, metadata
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
PROHIBITED = re.compile(r"(?:\bAGPL\b|\bSSPL\b|\bBUSL\b|BUSINESS SOURCE|COMMONS CLAUSE|(?<!L)\bGPL(?:-|\b))", re.I)


def _requirement_names(path: Path) -> list[str]:
    names: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = re.match(r"([A-Za-z0-9_.-]+)", line)
        if match:
            names.append(match.group(1))
    return names


def _python_licenses() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for name in _requirement_names(ROOT / "requirements.txt"):
        try:
            package = metadata(name)
        except PackageNotFoundError:
            rows.append({"name": name, "license": "MISSING"})
            continue
        license_value = package.get("License-Expression") or package.get("License") or ""
        if not license_value:
            classifiers = package.get_all("Classifier") or []
            license_value = "; ".join(item.rsplit("::", 1)[-1].strip() for item in classifiers if "License ::" in item)
        rows.append({"name": name, "license": license_value or "UNKNOWN"})
    return rows


def _node_licenses() -> list[dict[str, str]]:
    lock = json.loads((ROOT / "studio-frontend/package-lock.json").read_text(encoding="utf-8"))
    rows = []
    for package_path, package in (lock.get("packages") or {}).items():
        if not package_path:
            continue
        rows.append({"name": package_path.removeprefix("node_modules/"), "license": str(package.get("license") or "UNKNOWN")})
    return rows


def main() -> int:
    python_rows = _python_licenses()
    node_rows = _node_licenses()
    all_rows = python_rows + node_rows
    blocked = [row for row in all_rows if PROHIBITED.search(row["license"])]
    unresolved = [row for row in all_rows if row["license"] in {"UNKNOWN", "MISSING"}]
    print(json.dumps({
        "status": "passed" if not blocked and not unresolved else "failed",
        "python_packages": len(python_rows),
        "node_packages": len(node_rows),
        "blocked": blocked,
        "unresolved": unresolved,
        "optional_searxng": "AGPL service boundary; review obligations before distribution",
        "vendored_assets": {"manrope": "OFL-1.1", "mingcute": "Apache-2.0"},
    }, sort_keys=True))
    return 0 if not blocked and not unresolved else 1


if __name__ == "__main__":
    raise SystemExit(main())
