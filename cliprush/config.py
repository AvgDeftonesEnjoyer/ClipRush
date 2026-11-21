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
    auto_publish_on_approval: bool = False
    request_timeout_seconds: float = 60
    telegram_bot_token: str = ""
    telegram_owner_id: int = 0
    telegram_webhook_secret: str = ""
    discovery_chat_ids: str = ""
    kick_webhooks_enabled: bool = False
    youtube_api_key: str = ""
    twitch_client_id: str = ""
    twitch_client_secret: str = ""
    twitch_bot_nick: str = ""
    twitch_bot_oauth: str = ""
    openrouter_api_key: str = ""
    openrouter_model: str = ""
    openrouter_vision_model: str = ""
    acoustid_api_key: str = ""
    upload_post_api_key: str = ""
    data_dir: str = "/data"
    recovery_stale_minutes: int = 45
    scout_interval_seconds: int = 60
    min_post_gap_seconds: int = 7200
    max_post_day_new: int = 2
    max_post_day_established: int = 5
    max_download_bytes: int = 250_000_000
    budget_limit_usd: float = 80


@lru_cache
def get_settings() -> Settings:
    return Settings()


def verify_boot_configuration() -> None:
    """Reject sample credentials and incomplete active publishing config."""
    cfg = get_settings()
    token = cfg.admin_token.strip()
    if (len(token) < 16 or token.lower().startswith(("replace-", "change-me", "your-token"))
            or len(set(token)) < 8):
        raise RuntimeError("Configure a strong ADMIN_TOKEN before launching the API")
    if cfg.enable_publish and not cfg.upload_post_api_key:
        raise RuntimeError("ENABLE_PUBLISH requires UPLOAD_POST_API_KEY; fail closed")
