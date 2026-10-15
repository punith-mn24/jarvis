from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JARVIS_", env_file=".env", extra="ignore")

    mongodb_uri: SecretStr = SecretStr("mongodb://127.0.0.1:27017")
    mongodb_database: str = "jarvis"
    access_token: SecretStr = Field(min_length=24)
    step_delay: float = Field(default=0.6, ge=0, le=5)
    tool_timeout: float = Field(default=8, gt=0, le=15)
    retry_base: float = Field(default=0.5, ge=0, le=5)
    lease_seconds: int = Field(default=60, ge=30)
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:4b"
    ollama_timeout: float = Field(default=180, ge=5, le=300)
    email_address: str = ""
    email_password: SecretStr = SecretStr("")
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    smtp_security: Literal["ssl", "starttls"] = "ssl"
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    sent_folder: str = "[Gmail]/Sent Mail"
    email_timeout: float = Field(default=8, gt=0, le=15)
    browser_profile: str = ".local/jarvis-browser"
    browser_headless: bool = False
    brave_binary: str = ""
    firefox_binary: str = ""
    geckodriver_binary: str = ""
    firefox_profile: str = ".local/jarvis-firefox"
    brave_profile: str = ".local/jarvis-brave"

    @property
    def email_configured(self) -> bool:
        return bool(self.email_address and self.email_password.get_secret_value())


@lru_cache
def get_settings() -> Settings:
    return Settings()
