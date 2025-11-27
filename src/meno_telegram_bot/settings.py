from pydantic import Field, AnyHttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
        populate_by_name=True,
    )
    telegram_bot_token: str = Field(alias="TELEGRAM_BOT_TOKEN")
    backend_api_url: AnyHttpUrl = Field(alias="BACKEND_BASE_URL")


settings: Settings = Settings()  # type: ignore[call-arg]
