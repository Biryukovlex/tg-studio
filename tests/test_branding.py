"""Product-name contracts for TG Studio packaging and public UI."""

from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_public_brand_is_tg_studio():
    assert "# TG Studio" in _text("README.md")
    assert "TG Studio" in _text("app/web/templates/login.html")
    assert "TG Studio" in _text("app/web/static/favicon.svg")
    assert "TG Studio" in _text("app/bot.py")


def test_package_and_deployment_slug_is_tg_studio():
    package = json.loads(_text("studio-frontend/package.json"))
    compose = yaml.safe_load(_text("docker-compose.yml"))

    assert package["name"] == "tg-studio"
    assert compose["name"] == "tg-studio"
    assert {"tg-studio", "tg-studio-web", "tg-studio-worker"} <= set(compose["services"])
    assert "tg-studio.service" in _text("deploy/systemd/tg-studio.service")


def test_retired_product_names_do_not_return_to_public_branding():
    retired_names = (
        "TG Channel " + "Stats",
        "Telegram Content " + "Studio",
        "Telegram bot for " + "stats collection",
        "telegram-channel-" + "stats",
        "tg-" + "stats",
    )
    public_files = (
        "README.md",
        "app/bot.py",
        "app/web/routes.py",
        "app/web/templates/base.html",
        "app/web/templates/login.html",
        "docs/deployment.md",
        "studio-frontend/package.json",
        "docker-compose.yml",
    )

    public_text = "\n".join(_text(path) for path in public_files)
    for retired_name in retired_names:
        assert retired_name not in public_text
