from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def test_m5_artifact_panel_contract_is_built_and_responsive():
    source = (ROOT / "studio-frontend/src/main.tsx").read_text(encoding="utf-8")
    styles = (ROOT / "studio-frontend/src/styles.css").read_text(encoding="utf-8")
    bundle = (ROOT / "app/web/static/studio-dist/assets/studio.js").read_text(encoding="utf-8")
    assert "Copy for Telegram" in source
    assert "draft_conflict" in source or "Keep my text" in source
    assert "Restore" in source and "Source" in source
    assert "studio-draft.is-open" in styles
    assert "prefers-reduced-motion" in styles
    assert "Copy for Telegram" in bundle


def test_empty_thread_welcome_stays_inside_message_viewport():
    source = (ROOT / "studio-frontend/src/main.tsx").read_text(encoding="utf-8")
    styles = (ROOT / "studio-frontend/src/styles.css").read_text(encoding="utf-8")
    welcome_rule = re.search(r"\.studio-welcome\s*\{([^}]*)\}", styles)
    assert welcome_rule is not None
    assert "position: absolute" not in welcome_rule.group(1)
    assert '<ThreadPrimitive.Empty>' in source
    assert '<ThreadPrimitive.Messages components={{ Message: StudioMessage }} />' in source
    assert 'className="studio-viewport"' in source


def test_chat_viewport_scrolls_to_history_and_live_agent_activity_is_visible():
    source = (ROOT / "studio-frontend/src/main.tsx").read_text(encoding="utf-8")
    styles = (ROOT / "studio-frontend/src/styles.css").read_text(encoding="utf-8")
    viewport_rule = re.search(r"\.studio-viewport\s*\{([^}]*)\}", styles)
    assert viewport_rule is not None
    assert "display: flex" in viewport_rule.group(1)
    assert "flex-direction: column" in viewport_rule.group(1)
    assert "overflow-y: auto" in viewport_rule.group(1)
    assert "align-content: end" not in viewport_rule.group(1)
    assert ".studio-viewport > :first-child { margin-top: auto; }" in styles
    assert "function AgentActivity" in source
    assert 'role="status" aria-live="polite"' in source
    assert 'search_web: "Searching the web"' in source
    assert 'create_draft: "Building the draft artifact"' in source
    assert '`${label}…`' in source
    assert 'search_web: "Search results received"' in source
    assert 'create_draft: "Draft artifact saved"' in source
    assert 'return TOOL_ACTIVITY_RESULT_LABELS[normalized] ?? "Tool result received";' in source
    assert 'The agent may continue working.' in source
    assert '`${toolActivityLabel(toolName)} complete`' not in source
    assert '<AgentActivity run={recoveredRun} events={recoveredEvents} />' in source
    assert "onRunStartedEvent" in source
    assert "poll(runHint(input.runId" not in source
    assert ".studio-agent-activity" in styles


def test_studio_shell_keeps_the_composer_inside_the_viewport():
    studio_template = (ROOT / "app/web/templates/studio.html").read_text(encoding="utf-8")
    base_template = (ROOT / "app/web/templates/base.html").read_text(encoding="utf-8")
    shared_styles = (ROOT / "app/web/static/style.css").read_text(encoding="utf-8")
    studio_styles = (ROOT / "studio-frontend/src/styles.css").read_text(encoding="utf-8")
    assert "studio-page" in studio_template
    assert "block body_class" in base_template
    assert ".studio-page #main-content" in shared_styles
    assert ".studio-page #studio-react-root" in shared_styles
    assert ".studio-page .workspace > .topbar" in shared_styles
    assert ".studio-page .workspace > footer" in shared_styles
    main_rule = re.search(r"\.studio-main\s*\{([^}]*)\}", studio_styles)
    assert main_rule is not None
    assert "grid-template-rows: auto auto minmax(0, 1fr)" in main_rule.group(1)
    app_rule = re.search(r"\.studio-app\s*\{([^}]*)\}", studio_styles)
    assert app_rule is not None
    assert "height: 100%" in app_rule.group(1)
    assert "overflow: hidden" in app_rule.group(1)


def test_idle_studio_does_not_poll_an_empty_draft_and_has_a_favicon():
    source = (ROOT / "studio-frontend/src/main.tsx").read_text(encoding="utf-8")
    base_template = (ROOT / "app/web/templates/base.html").read_text(encoding="utf-8")
    login_template = (ROOT / "app/web/templates/login.html").read_text(encoding="utf-8")
    favicon = ROOT / "app/web/static/favicon.svg"
    assert "if (!conversationId || !watchForAgentChanges) return;" in source
    assert 'document.visibilityState === "hidden"' in source
    assert "refreshToken" in source and "onRunFinished" in source
    assert '/static/favicon.svg' in base_template
    assert '/static/favicon.svg' in login_template
    assert favicon.exists()


def test_artifact_panel_does_not_offer_publish_action():
    source = (ROOT / "studio-frontend/src/main.tsx").read_text(encoding="utf-8").lower()
    assert "publish" not in source


def test_assistant_messages_render_safe_gfm_while_user_messages_stay_literal():
    source = (ROOT / "studio-frontend/src/main.tsx").read_text(encoding="utf-8")
    styles = (ROOT / "studio-frontend/src/styles.css").read_text(encoding="utf-8")
    package = (ROOT / "studio-frontend/package.json").read_text(encoding="utf-8")
    assert 'from "@assistant-ui/react-markdown"' in source
    assert 'from "remark-gfm"' in source
    assert "<MessagePrimitive.If assistant>" in source
    assert "Text: StudioMarkdownText" in source
    assert "<MessagePrimitive.If user>" in source
    assert 'data-role={role}' in source
    assert source.count("Text: StudioMarkdownText") == 1
    assert 'target="_blank" rel="noopener noreferrer"' in source
    assert '"@assistant-ui/react-markdown"' in package
    assert '"remark-gfm"' in package
    assert ".studio-markdown h1" in styles
    assert ".studio-markdown blockquote" in styles
    assert ".studio-markdown pre" in styles
    assert '.studio-message[data-role="user"] p' in styles


def test_m5_backend_has_no_publish_route_or_agent_tool():
    routes = (ROOT / "app/studio/routes.py").read_text(encoding="utf-8").lower()
    agent = (ROOT / "app/studio/agent.py").read_text(encoding="utf-8").lower()
    assert not re.search(r'@router\.(get|post|patch|put|delete)\([^\n]*publish', routes)
    assert not re.search(r'async\s+def\s+publish\s*\(', agent)
