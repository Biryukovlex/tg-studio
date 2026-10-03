"""Product-name contracts for TG Studio packaging and public UI."""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]

# Frozen approved artwork (design-final-2026-10-01/manifest.json). Only these
# SVGs may live in app/web/static/brand/; the large mouthless PNG and the
# autosprite source stay out of the product bundle (their approved pixels are
# already embedded in the SVGs).
APPROVED_GHOST_ASSETS = {
    "tgstudio-ghost-overview.svg": "1ed4463b772f81c07076166202c1364a0b23a2360ffadee011c07fb3fe345c4c",
    "tgstudio-ghost-settings.svg": "83d99e6c6fc54dcf86b97716f9ca44b97e2597daece1020fe4d066436e3a0c70",
    "tgstudio-ghost-studio.svg": "841f17f38c5a695ca4f92577d255491088c31f58c409cbf8c85eaed424cae6b3",
}

# Review scaffolding from the private prototype; never product code.
REVIEW_CONTROL_MARKERS = (
    "play-example",
    "motion-switch",
    "demo-fill",
    "States review",
    "fictional-data",
)


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


# --- T50 shared shell, brand and mascot contracts ---


def test_shell_has_single_ghost_and_static_wordmark():
    base = _text("app/web/templates/base.html")
    assert base.count('id="ghost-trigger"') == 1
    assert base.count("ghost-trigger") <= 4
    assert 'class="ghost-scene"' not in base  # injected at runtime, never duplicated
    assert 'data-ghost-mode="{{ ghost_mode }}"' in base
    assert "tgstudio-ghost-{{ ghost_mode }}.svg" in base
    assert 'class="brand-wordmark"' in base
    assert "<span>TG</span>Studio</a>" in base
    assert 'aria-label="Replay ghost animation"' in base
    # Per-page action mode without backend changes.
    assert "startswith('/studio')" in base
    assert "startswith('/settings')" in base


def test_shell_keeps_auth_logout_and_blocks():
    base = _text("app/web/templates/base.html")
    assert 'href="/logout"' in base
    assert "can_manage_settings" in base
    assert "{% block content %}{% endblock %}" in base
    assert "{% block pageheading %}" in base
    assert "{% block navactions %}" in base
    assert "/static/ghost.js" in base
    assert "/static/ui-controls.js" in base
    assert "/static/app.js" in base


def test_login_keeps_auth_and_uses_approved_brand():
    login = _text("app/web/templates/login.html")
    assert "TG Studio" in login
    assert 'action="/login"' in login
    assert 'name="password"' in login
    assert "/static/brand/tgstudio-ghost-overview.svg" in login
    assert "brand-lockup" in login


def test_review_controls_absent_from_product_shell():
    product_text = "\n".join(
        _text(path)
        for path in (
            "app/web/templates/base.html",
            "app/web/templates/login.html",
            "app/web/static/style.css",
            "app/web/static/ghost.js",
            "app/web/static/ui-controls.js",
        )
    )
    for marker in REVIEW_CONTROL_MARKERS:
        assert marker not in product_text


def test_brand_directory_holds_only_approved_assets():
    brand_dir = ROOT / "app" / "web" / "static" / "brand"
    assert brand_dir.is_dir()
    names = sorted(path.name for path in brand_dir.iterdir() if path.is_file())
    assert names == sorted(APPROVED_GHOST_ASSETS)
    for name, want in APPROVED_GHOST_ASSETS.items():
        digest = hashlib.sha256((brand_dir / name).read_bytes()).hexdigest()
        assert digest == want, name


def test_ghost_assets_are_unique_approved_scenes():
    seen_ids: set[str] = set()
    for name in APPROVED_GHOST_ASSETS:
        svg = _text(f"app/web/static/brand/{name}")
        ET.fromstring(svg)  # must parse as XML
        mode = name.removeprefix("tgstudio-ghost-").removesuffix(".svg")
        assert f'data-mode="{mode}"' in svg
        assert 'role="img"' in svg
        assert "TGStudio paper ghost" in svg
        # Approved dark-surface compositing: lighten blend plus the exact
        # dark-backdrop removal filter; no backing tile.
        assert "mix-blend-mode" in svg and "lighten" in svg
        assert "feColorMatrix" in svg
        assert "2 2 2 0 -1" in svg
        # Rejected older mouth strokes must not return.
        assert "mouth" not in svg.lower()
        # Doubled clear magnifier: 72-unit radius (144-unit diameter).
        assert 'r="72"' in svg
        # All three action durations plus the idle gaze share one scene file.
        assert "6.2s" in svg and "8.6s" in svg and "3.4s" in svg
        assert "ghost-look-down" in svg
        ids = re.findall(r'id="([^"]+)"', svg)
        assert len(ids) == len(set(ids)) == 10
        assert all(i.startswith(f"ghost-{mode}-") for i in ids)
        assert not (set(ids) & seen_ids)
        seen_ids.update(ids)


def test_ghost_lifecycle_single_play_idle_and_cleanup():
    ghost = _text("app/web/static/ghost.js")
    for marker in (
        "data-playing",
        "data-looking",
        "animationend",
        "visibilitychange",
        "hashchange",
        "prefers-reduced-motion",
        "clearTimeout",
        "removeEventListener",
        "destroy",
        "ghostReady",
        "data-mascot-paused",
    ):
        assert marker in ghost, marker
    # Rapid pokes replace rather than queue; no polling loops or demo timers.
    assert "setInterval" not in ghost
    for marker in REVIEW_CONTROL_MARKERS:
        assert marker not in ghost


def test_shared_dropdown_and_dialog_primitives():
    controls = _text("app/web/static/ui-controls.js")
    for marker in (
        'aria-haspopup="listbox"',
        'role", "listbox"',
        "aria-expanded",
        "aria-controls",
        "aria-selected",
        "ArrowDown",
        "ArrowUp",
        "Home",
        "End",
        "Escape",
        "opens-up",
        "showModal",
        "data-ui-dialog-open",
        "data-ui-dialog-close",
        ".focus()",
    ):
        assert marker in controls, marker


def test_shell_css_covers_junction_ghost_controls_and_motion():
    css = _text("app/web/static/style.css")
    for marker in (
        ".brand-junction",
        ".ghost-trigger",
        ".ghost-fallback",
        ".brand-wordmark",
        ".brand-lockup",
        ".ui-select",
        ".ui-select-menu",
        ".opens-up",
        ".ui-dialog",
        "data-mascot-paused",
        "mix-blend-mode: lighten",
        "--junction-height: 88px",
        "@media (max-width: 680px)",
        "@media (max-width: 380px)",
        "@media (prefers-reduced-motion: reduce)",
    ):
        assert marker in css, marker
    # No logo tile: the ghost trigger stays transparent.
    assert "background: transparent" in css
    assert "ghost-tile" not in css and "logo-tile" not in css
