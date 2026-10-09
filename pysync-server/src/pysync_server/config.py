from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PYSYNC_SERVER_", env_file=None)
    data_dir: Path = Path(".pysync-data")
    owner_token: SecretStr = SecretStr("")
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    max_file_size: int = Field(default=100 * 1024 * 1024, ge=1)
    max_storage_size: int = Field(default=20 * 1024**3, ge=1)
    max_uploads: int = Field(default=4, ge=1, le=32)
    upload_timeout: float = Field(default=300, ge=1, le=3600)
    tls_cert: str | None = None
    tls_key: str | None = None
