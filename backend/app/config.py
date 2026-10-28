"""Application settings.

Values come from backend/.env (copy backend/.env.example) or environment variables. Only the database
connection and JWT_SECRET are required; everything else has a sensible local default.
Secrets (ORACLE_PASSWORD, JWT_SECRET) are SecretStr so they never appear in repr() or logs.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    # Oracle
    oracle_dsn: str = Field(..., description="host:port/service, e.g. localhost:1531/FREEPDB1")
    oracle_user: str
    oracle_password: SecretStr
    oracle_pool_min: int = 1
    oracle_pool_max: int = 4
    oracle_pool_increment: int = 1
    oracle_ssl_server_dn_match: bool = True
    oracle_call_timeout_ms: int = 600_000

    # Auth
    jwt_secret: SecretStr
    jwt_ttl_minutes: int = 60
    require_four_eyes_approval: bool = False
    cors_origins: str = "http://localhost:3000"

    # Local storage (created on first use; git-ignored)
    data_dir: Path = PROJECT_DIR / "local_data"             # uploaded CSVs and generated exports
    model_dir: Path = PROJECT_DIR / "local_data" / "models"  # trained model artifacts, one folder per version

    # ML / scheduling
    timezone: str = "Asia/Colombo"
    training_cron_hour: int = 2
    training_cron_minute: int = 0
    history_months: int = 12
    aggregation_months: int = 3
    reserve_depth: int = 6
    promotion_tolerance: float = 0.02
    training_max_attempts: int = 3
    training_retry_delay_seconds: int = 300
    lock_lease_seconds: int = 4 * 3600
    worker_poll_seconds: int = 15
    export_ttl_hours: int = 72
    dismissal_suppression_days: int = 30

    log_level: str = "INFO"

    @field_validator("oracle_dsn")
    @classmethod
    def _dsn_not_placeholder(cls, v: str) -> str:
        if "<" in v or not v.strip():
            raise ValueError("ORACLE_DSN is not configured")
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def import_dir(self) -> Path:
        return self.data_dir / "imports"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "exports"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
