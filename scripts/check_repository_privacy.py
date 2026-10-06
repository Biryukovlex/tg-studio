"""Reject private runtime/local-agent files in the Git index (including tests).

Credential content is checked separately with Gitleaks. This path gate never
opens a private file or prints its contents.
"""
from __future__ import annotations

import subprocess
from pathlib import PurePosixPath

PRIVATE_DIRECTORIES = {
    ".agent-workspace", ".agents", ".codex", ".claude", ".opencode",
    "data", "backups", "output", "tg-studio-private-specifications",
    ".venv", "venv", ".ci-venv", "node_modules",
}
PRIVATE_FILENAMES = {
    "AGENTS.md", "CLAUDE.md", "opencode.json", "credentials.json",
    "auth.json", "settings.local.json", ".secret", ".DS_Store",
}
PRIVATE_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".dump", ".session", ".log")


def private_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else ""
    if any(part in PRIVATE_DIRECTORIES for part in parts):
        return True
    if name in PRIVATE_FILENAMES:
        return True
    if (name == ".env" or name.startswith(".env.")) and name != ".env.example":
        return True
    return name.endswith(PRIVATE_SUFFIXES) or ".session-" in name or ".db-" in name


def main() -> int:
    paths = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")
    blocked = sorted(path for path in paths if path and private_path(path))
    if blocked:
        print("Private files must not be committed:")
        for path in blocked:
            print(f"  {path}")
        return 1
    print("Repository privacy paths OK (runtime data and local settings excluded).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
