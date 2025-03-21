from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    telegram_bot_token: str
    backend_api_url: str = "http://localhost:8000/chat"

    class Config:
        env_file = ".env"


settings = Settings()
