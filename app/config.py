from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    SECRET_KEY: str = "dev-secret-change-me"

    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""

    PI_EMAIL: str = ""
    ADMIN_PASSWORD: str = ""
    ALLOWED_EMAIL_DOMAINS: str = ""
    DEFAULT_QUOTA_HOURS: float = 40.0

    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "GPU Manager <noreply@localhost>"
    EMAIL_ENABLED: bool = True

    QUEUE_CONFIRM_MINUTES: int = 15
    MAX_RESERVATION_HOURS: int = 24
    WARN_BEFORE_END_MINUTES: int = 15

    BASE_URL: str = "http://localhost:8000"

    DATABASE_URL: str = "sqlite:///./gpu_manager.db"

    @property
    def allowed_domains_list(self) -> list[str]:
        if not self.ALLOWED_EMAIL_DOMAINS.strip():
            return []
        return [d.strip().lower() for d in self.ALLOWED_EMAIL_DOMAINS.split(",") if d.strip()]


settings = Settings()
