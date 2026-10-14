"""Application settings, read once from environment variables (and `.env` when present)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Fixed role names. They are created by deploy/postgres/init-roles.sh and referenced
# by the migrations; only their passwords are configurable.
DB_OWNER_ROLE = "medvora_owner"
DB_APP_ROLE = "medvora_app"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["local", "test", "prod"] = "local"
    log_level: str = "INFO"

    db_host: str = "127.0.0.1"
    db_port: int = 5432
    db_name: str = "medvora"
    db_app_password: SecretStr
    # Only the migrate/tools containers get the owner password; the API and worker never do.
    db_owner_password: SecretStr | None = None
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_pool_timeout_seconds: float = 10.0
    db_connect_timeout_seconds: int = 5

    # Server-side secret mixed into API key hashes, so a leaked database alone is useless.
    api_key_pepper: SecretStr

    mappings_dir: Path = Path("mappings")
    problem_type_base: str = "https://medvora.dev/problems/"

    max_request_bytes: int = 1 * 1024 * 1024
    upload_max_bytes: int = 20 * 1024 * 1024
    upload_max_rows: int = 100_000

    job_runner: Literal["worker"] = "worker"
    job_chunk_size: int = 500
    job_stale_after_seconds: int = 300
    job_max_attempts: int = 3
    worker_poll_interval_seconds: float = 1.0
    # Waits between attempts after a transient failure: 3 retries, then give up.
    transient_retry_delays: tuple[float, ...] = (1.0, 4.0, 16.0)

    rate_limit_records_per_minute: int = 100
    rate_limit_uploads_per_hour: int = 10

    # The last check before an automatic match; it can only veto. "llm" is optional.
    match_verifier: Literal["rule", "llm"] = "rule"
    match_verifier_timeout_seconds: float = 20.0
    match_verifier_model: str = "claude-opus-5-5"
    anthropic_api_key: SecretStr | None = None

    @property
    def database_url(self) -> str:
        return self._url(DB_APP_ROLE, self.db_app_password)

    @property
    def migration_database_url(self) -> str:
        if self.db_owner_password is None:
            raise RuntimeError("DB_OWNER_PASSWORD is required for migrations and admin scripts")
        return self._url(DB_OWNER_ROLE, self.db_owner_password)

    def _url(self, user: str, password: SecretStr) -> str:
        secret = quote(password.get_secret_value(), safe="")
        return f"postgresql+psycopg://{user}:{secret}@{self.db_host}:{self.db_port}/{self.db_name}"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # required fields come from the environment
