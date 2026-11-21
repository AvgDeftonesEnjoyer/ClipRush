from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./cliprush.db"
    redis_url: str = "redis://localhost:6379/0"
    admin_token: str = ""
    openshorts_api_url: str = "http://localhost:8000"
    openshorts_api_key: str = ""
    openshorts_webhook_secret: str = ""
    public_base_url: str = ""
    enable_publish: bool = False
    request_timeout_seconds: float = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
