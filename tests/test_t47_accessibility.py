"""T47 cross-screen responsive/keyboard/contrast/zoom refinement checks.

Computed (not eyeballed) contrast for secondary text, global reduced-motion
handling on both shells, visible focus on every shell, Tab isolation inside
all modal dialogs, viewport-fit dialog/menu widths, phone card layouts
without document panning, and long-content wrapping. Live-device keyboard,
safe-area and 200% zoom observation remain T49 work and are labelled there.
"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB_CSS = (ROOT / "app/web/static/style.css").read_text(encoding="utf-8")
STUDIO_CSS = (ROOT / "studio-frontend/src/styles.css").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/web/static/app.js").read_text(encoding="utf-8")
MAIN_TSX = (ROOT / "studio-frontend/src/main.tsx").read_text(encoding="utf-8")
PROFILE_TSX = (ROOT / "studio-frontend/src/ChannelProfileDialog.tsx").read_text(encoding="utf-8")
DIALOG_FOCUS = (ROOT / "studio-frontend/src/dialogFocus.ts").read_text(encoding="utf-8")


def _luminance(hex_color: str) -> float:
    hex_color = hex_color.lstrip("#")
    red, green, blue = (int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def _ratio(foreground: str, background: str) -> float:
    light, dark = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def _web_var(name: str) -> str:
    match = re.search(rf"{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})", WEB_CSS)
    assert match, f"CSS variable {name} must exist"
    return match.group(1)


def test_secondary_text_clears_aa_on_surfaces():
    faint = _web_var("--faint")
    assert _ratio(faint, _web_var("--surface")) >= 4.5
    assert _ratio(_web_var("--muted"), _web_var("--surface")) >= 4.5
    assert _ratio(_web_var("--soft-text"), _web_var("--surface")) >= 4.5
    assert _ratio(_web_var("--cyan"), _web_var("--surface")) >= 4.5
    assert _ratio(_web_var("--coral"), _web_var("--surface")) >= 4.5
    assert _ratio(_web_var("--blue"), _web_var("--surface")) >= 4.5


def test_reduced_motion_is_handled_on_both_shells():
    assert "@media (prefers-reduced-motion: reduce)" in WEB_CSS
    assert "@media (prefers-reduced-motion: reduce)" in STUDIO_CSS
    assert "prefers-reduced-motion: reduce" in APP_JS
    assert "motionOK" in APP_JS


def test_visible_focus_everywhere():
    assert ":focus-visible" in WEB_CSS
    assert ":focus-visible" in STUDIO_CSS


def test_modal_dialogs_trap_tab_and_return_focus():
    # Server shell: one delegated Tab loop over the topmost open dialog.
    assert "dialog[open]" in APP_JS
    assert "trap" in APP_JS.lower() or "Modal isolation" in APP_JS
    # Studio shell: shared hook wired into every modal dialog.
    assert "trapTabInDialog" in DIALOG_FOCUS
    assert "useDialogFocusTrap" in DIALOG_FOCUS
    assert MAIN_TSX.count("useDialogFocusTrap(dialog)") >= 1
    assert PROFILE_TSX.count("useDialogFocusTrap(dialogRef)") == 2
    # Escape and focus return stay on each dialog.
    assert "onCancel" in PROFILE_TSX
    assert "readerDialog.addEventListener('close'" in APP_JS


def test_dialogs_and_menus_fit_narrow_viewports():
    assert "min(760px, 94vw)" in WEB_CSS
    assert "min(700px, calc(100vw - 32px))" in STUDIO_CSS
    assert "min(280px, calc(100vw - 32px))" in STUDIO_CSS


def test_phone_layouts_avoid_document_panning():
    assert "@media (max-width: 680px)" in WEB_CSS
    assert ".explorer-table thead { display: none; }" in WEB_CSS
    assert "min-width: 320px" in WEB_CSS or "min-width:320px" in WEB_CSS
    assert "min-width: 320px" in STUDIO_CSS or "min-width:320px" in STUDIO_CSS


def test_long_user_content_wraps():
    assert ".comment-text" in WEB_CSS
    assert ".reader-discussion li" in WEB_CSS
    assert WEB_CSS.count("overflow-wrap: anywhere") >= 3
