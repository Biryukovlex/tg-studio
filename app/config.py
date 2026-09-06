from functools import cached_property
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Telegram API
    api_id: int = 0
    api_hash: str = ""
    session_string: str = ""

    # Channels
    channels: str = ""  # comma separated

    # Web admin panel
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    admin_username: str = "admin"
    admin_password: str = ""
    session_secret: str = ""
    # Runtime process topology. ``all`` is the community default; hosted
    # deployments can run the web and Telegram collector independently.
    process_role: str = "all"

    # Content Studio (kept disabled until explicitly enabled)
    studio_enabled: bool = False
    openrouter_api_key: str = ""
    openrouter_model: str = "openai/gpt-4o-mini"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # M2 agent bounds. ``studio_test_mode`` is an explicit deterministic test
    # seam and must never be enabled in a hosted deployment.
    studio_test_mode: bool = False
    studio_provider_timeout_seconds: float = 45.0
    studio_run_timeout_seconds: float = 300.0
    studio_run_concurrency: int = 2
    studio_run_lease_seconds: int = 120
    studio_run_heartbeat_seconds: float = 20.0
    studio_queued_run_grace_seconds: int = 60
    studio_tool_timeout_seconds: float = 30.0
    studio_tool_concurrency: int = 6
    # Research may use channel context, several query batches, source reads,
    # and comparison. Twelve remains bounded but permits a real search loop.
    studio_max_tool_calls: int = 12
    # This is cumulative across the whole agent/tool loop, not only the final
    # answer. Iterative research needs headroom even when the final is concise.
    studio_max_output_tokens: int = 8192
    # M3 evidence/context bounds. These are server-side limits and are never
    # accepted from browser requests.
    studio_context_max_chars: int = 18_000
    studio_max_evidence_posts: int = 20
    studio_min_profile_posts: int = 5
    # M4 research is deliberately optional and failure-isolated.  Search is
    # only enabled when an operator points the app at a private SearXNG
    # instance; an empty URL always produces a visible degraded state.
    studio_search_enabled: bool = False
    studio_search_provider: str = "searxng"
    studio_search_base_url: str = ""
    studio_search_timeout_seconds: float = 10.0
    studio_search_retries: int = 1
    # The agent can choose a bounded engine mix per search.  This value is the
    # fallback when it does not specify one; the allow-list prevents arbitrary
    # engine names from becoming an unbounded provider surface.
    studio_search_engines: str = "yandex,github,arxiv,wikipedia"
    studio_search_allowed_engines: str = "yandex,github,arxiv,wikipedia,bing,bing news,google,google news,brave,brave news,mojeek,qwant"
    studio_search_blocked_domains: str = ""
    studio_search_max_queries: int = 12
    studio_search_max_results: int = 10
    studio_search_cache_ttl_seconds: int = 900
    studio_source_reader_enabled: bool = True
    studio_source_timeout_seconds: float = 10.0
    studio_source_max_bytes: int = 1_000_000
    studio_source_max_redirects: int = 3
    studio_source_max_chars: int = 12_000

    # Telegram commands
    admin_tg_ids: str = ""

    # Collection tuning
    poll_minutes: float = 15.0
    track_days: int = 30  # 0 = entire channel history
    backfill_limit: int = 200  # 0 = no message-count limit

    # Storage
    data_dir: str = "data"
    # PostgreSQL is the M1+ runtime store.  Leaving this empty preserves the
    # pre-M1 local SQLite mode until the operator completes the import.
    database_url: str = ""
    app_mode: str = "community"
    local_workspace_slug: str = "community"
    database_pool_size: int = 5
    database_max_overflow: int = 5
    database_pool_timeout: float = 30.0
    database_pool_recycle: int = 1800
    telegram_session_encryption_key: str = ""
    telegram_connection_label: str = "default"

    @field_validator("api_id", mode="before")
    @classmethod
    def _blank_int_is_zero(cls, v):  # tolerate `API_ID=` left empty in .env
        return 0 if v in ("", None) else v

    @property
    def channel_list(self) -> list[str]:
        return [c.strip() for c in self.channels.split(",") if c.strip()]

    @cached_property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def db_path(self) -> Path:
        return self.data_path / "stats.db"

    @cached_property
    def admin_ids(self) -> set[int]:
        out: set[int] = set()
        for part in self.admin_tg_ids.replace(";", ",").split(","):
            part = part.strip()
            if part:
                try:
                    out.add(int(part))
                except ValueError:
                    pass
        return out

    def validate_required(self) -> list[str]:
        problems: list[str] = []
        role = str(self.process_role or "all").strip().lower()
        if role not in {"all", "web", "worker"}:
            problems.append("PROCESS_ROLE must be one of: all, web, worker.")
        needs_telegram = role in {"all", "worker"}
        needs_admin = role in {"all", "web"}
        if needs_telegram and (not self.api_id or not self.api_hash):
            problems.append("API_ID / API_HASH missing - get them at https://my.telegram.org (API development tools).")
        # In PostgreSQL mode the encrypted connection row may be the source of
        # truth after the first login.  The main process still fails clearly if
        # neither that row nor an environment session is available.
        if needs_telegram and not self.session_string and not (self.postgres_enabled and self.telegram_session_encryption_key):
            problems.append("SESSION_STRING empty - run `python scripts/generate_session.py` once, then paste it into .env.")
        if needs_telegram and not self.channel_list:
            problems.append("CHANNELS empty - e.g. CHANNELS=@my_channel")
        if needs_admin and not self.admin_password:
            problems.append("ADMIN_PASSWORD empty - required for the web admin panel.")
        return problems

    @property
    def postgres_enabled(self) -> bool:
        return bool(self.database_url.strip())

    def validate_postgres(self) -> list[str]:
        """Return PostgreSQL-only configuration errors without affecting Studio."""
        problems: list[str] = []
        if self.postgres_enabled and not self.database_url.startswith(("postgres://", "postgresql://", "postgresql+asyncpg://")):
            problems.append("DATABASE_URL must use postgres://, postgresql://, or postgresql+asyncpg://")
        if self.postgres_enabled and self.telegram_session_encryption_key and len(self.telegram_session_encryption_key) < 32:
            problems.append("TELEGRAM_SESSION_ENCRYPTION_KEY must be at least 32 characters")
        return problems


def load_settings() -> Settings:
    s = Settings()
    s.data_path.mkdir(parents=True, exist_ok=True)
    return s
