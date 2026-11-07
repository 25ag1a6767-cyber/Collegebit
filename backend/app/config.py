"""Environment-based application settings."""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import model_validator

class Settings(BaseSettings):
    app_name: str = "ACE Canteen"
    brand_name: str = "CollegeBits"
    app_version: str = "2.0.0"
    environment: str = "development"
    log_level: str = "INFO"
    # Primary deployment uses self-hosted PostgreSQL on college-owned hardware (or Neon Free PostgreSQL for cloud dev).
    # SQLite gives a zero-configuration local/test fallback.
    database_url: str = "sqlite:///./backend/ace_canteen.db"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 30
    db_pool_recycle_seconds: int = 1800
    db_use_pgbouncer_transaction_pool: bool = False
    db_statement_timeout_ms: int = 15000
    auto_create_tables: bool = True
    seed_demo_data: bool = True
    secret_key: str = "change-this-development-secret-before-deployment"
    access_token_minutes: int = 720
    refresh_token_days: int = 14
    pickup_token_minutes: int = 20
    checkout_reservation_minutes: int = 30
    admin_require_mfa: bool = False
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    payment_currency: str = "INR"
    # AI assistant (Google Gemini API, optional — deterministic verified-menu engine used when empty)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    # Optional Web Push VAPID configuration
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = "mailto:canteen@example.edu"
    # Optional S3-compatible object storage configuration
    object_storage_bucket: str = ""
    object_storage_public_url: str = ""
    # CORS origins for mobile / separate web builds (comma-separated; empty = same-origin only in production)
    cors_origins: str = ""
    # Public base URL used in emailed links (no trailing slash), e.g. https://canteen.example.edu
    public_base_url: str = "http://127.0.0.1:8000"
    password_reset_minutes: int = 30
    email_verification_required: bool = False
    email_receipts_enabled: bool = True
    # SMTP is optional. Without it, password-reset emails are only logged in development.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_use_ssl: bool = False
    email_order_ready: bool = True  # email the student when their order is ready (needs SMTP in production)
    # Canteen hours (24h HH:MM, in CANTEEN_TIMEZONE). Leave both empty for "always open, no pickup slots".
    canteen_open_time: str = ""
    canteen_close_time: str = ""
    canteen_timezone: str = "Asia/Kolkata"
    canteen_open_days: str = "0,1,2,3,4,5"  # Monday=0 ... Sunday=6
    pickup_slot_minutes: int = 15
    pickup_slot_capacity: int = 10  # max orders per pickup slot
    pickup_min_lead_minutes: int = 10
    overdue_preparation_minutes: int = 20
    # Optional shared store for login/reset throttling and response caching so several app workers share state.
    redis_url: str = ""
    api_rate_limit_per_minute: int = 300
    # Four-day hybrid data retention & encrypted GFS backup / disaster recovery policy
    temp_retention_days: int = 4
    backup_primary_dir: str = "backups/primary"
    backup_secondary_dir: str = "backups/secondary_offhost"
    backup_encryption_key: str = ""
    backup_max_age_hours: int = 36
    backup_keep_daily: int = 7
    backup_keep_weekly: int = 4
    backup_keep_monthly: int = 3
    automated_daily_report_emails: str = ""
    model_config = SettingsConfigDict(env_file=(".env", "backend/.env"), env_file_encoding="utf-8", case_sensitive=False, extra="ignore")

    @model_validator(mode="after")
    def validate_security_settings(self):
        # Normalize relative SQLite paths so the app always finds its db regardless of current working directory
        import os
        if (os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")) and self.database_url.startswith("sqlite"):
            self.database_url = "sqlite:////tmp/ace_canteen.db"
            self.auto_create_tables = True
            self.seed_demo_data = True
        elif self.database_url.startswith("sqlite:///.") or self.database_url.startswith("sqlite:///backend/"):
            from pathlib import Path
            project_root = Path(__file__).resolve().parent.parent.parent
            rel_part = self.database_url.replace("sqlite:///", "")
            if rel_part.startswith("./"):
                rel_part = rel_part[2:]
            abs_db = (project_root / rel_part).resolve().as_posix()
            self.database_url = f"sqlite:///{abs_db}"

        if self.access_token_minutes < 1 or self.access_token_minutes > 10080:
            raise ValueError("ACCESS_TOKEN_MINUTES must be between 1 and 10080")
        if self.refresh_token_days < 1 or self.refresh_token_days > 90:
            raise ValueError("REFRESH_TOKEN_DAYS must be between 1 and 90")
        if self.pickup_token_minutes < 2 or self.pickup_token_minutes > 1440:
            raise ValueError("PICKUP_TOKEN_MINUTES must be between 2 and 1440")
        if self.checkout_reservation_minutes < 5 or self.checkout_reservation_minutes > 120:
            raise ValueError("CHECKOUT_RESERVATION_MINUTES must be between 5 and 120")
        if bool(self.canteen_open_time) != bool(self.canteen_close_time):
            raise ValueError("Set both CANTEEN_OPEN_TIME and CANTEEN_CLOSE_TIME, or neither")
        if self.canteen_open_time:
            from datetime import time as _t
            from zoneinfo import ZoneInfo
            try:
                o = _t.fromisoformat(self.canteen_open_time); c = _t.fromisoformat(self.canteen_close_time)
                ZoneInfo(self.canteen_timezone)
                days = {int(x) for x in self.canteen_open_days.split(",") if x.strip()}
            except Exception:
                raise ValueError("Canteen hours must be HH:MM, CANTEEN_TIMEZONE a valid zone, CANTEEN_OPEN_DAYS like 0,1,2,3,4,5")
            if o >= c or not days or any(d < 0 or d > 6 for d in days):
                raise ValueError("CANTEEN_OPEN_TIME must be before CANTEEN_CLOSE_TIME and open days must be 0-6")
        if not 5 <= self.pickup_slot_minutes <= 120 or self.pickup_slot_capacity < 1 or not 0 <= self.pickup_min_lead_minutes <= 240:
            raise ValueError("Invalid pickup slot settings")
        if self.password_reset_minutes < 5 or self.password_reset_minutes > 120:
            raise ValueError("PASSWORD_RESET_MINUTES must be between 5 and 120")
        if self.temp_retention_days < 1 or self.temp_retention_days > 365:
            raise ValueError("TEMP_RETENTION_DAYS must be between 1 and 365")
        if len(self.secret_key) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters long")
        if self.environment.lower() in {"production", "prod"}:
            if self.secret_key == "change-this-development-secret-before-deployment":
                raise ValueError("Set a unique SECRET_KEY before running in production")
            if self.auto_create_tables:
                raise ValueError("AUTO_CREATE_TABLES must be false in production; run Alembic migrations explicitly")
            if self.seed_demo_data:
                raise ValueError("SEED_DEMO_DATA must be false in production")
            if not self.public_base_url.startswith("https://"):
                raise ValueError("PUBLIC_BASE_URL must be an https:// URL in production")
        return self

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
