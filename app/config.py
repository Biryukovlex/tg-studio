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
    behind_tls: bool = False
    trusted_proxy_ips: str = ""
    # Runtime process topology. ``all`` is the community default; hosted
    # deployments can run the web and Telegram collector independently.
    process_role: str = "all"

    # Content Studio
    openrouter_api_key: str = ""
    openrouter_model: str = "openai/gpt-4o-mini"
    # ``studio_test_mode`` is an explicit deterministic test seam and must
    # never be enabled in a hosted deployment.
    studio_test_mode: bool = False
    studio_search_enabled: bool = False
    studio_search_base_url: str = ""
    studio_search_blocked_domains: str = ""

    # Collection tuning
    poll_minutes: float = 15.0
    track_days: int = 30  # 0 = entire channel history
    backfill_limit: int = 200  # 0 = no message-count limit

    # Storage
    data_dir: str = "data"
    # PostgreSQL is the M1+ runtime store.  Leaving this empty preserves the
    # pre-M1 local SQLite mode until the operator completes the import.
    database_url: str = ""
    telegram_session_encryption_key: str = ""

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

    def validate_required(
        self,
        db_channels: list | None = None,
        *,
        defer_telegram: bool = False,
    ) -> list[str]:
        problems: list[str] = []
        role = str(self.process_role or "all").strip().lower()
        if role not in {"all", "web", "worker"}:
            problems.append("PROCESS_ROLE must be one of: all, web, worker.")
        needs_telegram = role in {"all", "worker"}
        needs_admin = role in {"all", "web"}
        if needs_telegram and not defer_telegram and (not self.api_id or not self.api_hash):
            problems.append("API_ID / API_HASH missing - get them at https://my.telegram.org (API development tools).")
        # In PostgreSQL mode the encrypted connection row may be the source of
        # truth after the first login.  The main process still fails clearly if
        # neither that row nor an environment session is available.
        if needs_telegram and not defer_telegram and not self.session_string:
            problems.append("SESSION_STRING empty - run `python scripts/generate_session.py` once, then paste it into .env.")
        if needs_telegram and not self.channel_list:
            # In PostgreSQL mode, CHANNELS may be empty if the database already has channels
            if db_channels is not None:
                if not db_channels:
                    problems.append("CHANNELS empty - e.g. CHANNELS=@my_channel")
            elif self.postgres_enabled:
                # When postgres is enabled but we haven't checked DB yet, don't require CHANNELS here;
                # main.py will re-validate after DB init with actual channels
                pass
            else:
                problems.append("CHANNELS empty - e.g. CHANNELS=@my_channel")
        if needs_admin and not self.admin_password:
            problems.append("ADMIN_PASSWORD empty - required for the web admin panel.")
        # Guard against shipping default credentials on a public interface.
        if needs_admin and self.admin_password == "change-me":
            host = (self.web_host or "").strip().lower()
            loopback_hosts = {"127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"}
            if host not in loopback_hosts:
                problems.append("ADMIN_PASSWORD must be changed from 'change-me' when WEB_HOST is not loopback.")
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
