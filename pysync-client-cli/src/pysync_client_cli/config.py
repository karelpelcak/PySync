import json
import os
from pathlib import Path
from urllib.parse import urlparse

from platformdirs import user_config_path
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_IGNORES = [
    ".git/",
    ".venv/",
    "node_modules/",
    "__pycache__/",
    ".pytest_cache/",
    ".DS_Store",
    "*.pyc",
    ".env",
    ".env.*",
    ".pysync-*/",
]


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PYSYNC_CLIENT_", extra="forbid")
    server_url: str = "http://127.0.0.1:8000"
    allow_insecure: bool = False
    device_name: str = ""
    device_id: str = ""
    token: SecretStr = SecretStr("")
    debounce_ms: int = Field(default=500, ge=50, le=30000)
    poll_seconds: float = Field(default=3, ge=0.1, le=300)
    max_file_size: int = Field(default=100 * 1024 * 1024, ge=1)
    ignore: list[str] = Field(default_factory=lambda: DEFAULT_IGNORES.copy())

    @field_validator("server_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError("Server URL must be an http(s) origin without credentials or a path")
        return value.rstrip("/")

    def check_transport(self) -> None:
        parsed = urlparse(self.server_url)
        if (
            parsed.scheme == "http"
            and parsed.hostname not in ("localhost", "127.0.0.1", "::1")
            and not self.allow_insecure
        ):
            raise ValueError(
                "Use HTTPS, or explicitly enable --allow-insecure for a trusted VPN/LAN"
            )


def config_dir() -> Path:
    return Path(os.environ.get("PYSYNC_CONFIG_DIR", user_config_path("pysync"))).absolute()


def load_config(directory: Path) -> Config:
    path = directory / "config.json"
    if path.is_symlink():
        raise ValueError("Credential file must not be a symlink")
    if path.exists():
        if path.stat().st_mode & 0o077:
            raise ValueError(f"Credentials require private permissions: chmod 600 {path}")
        config = Config(**json.loads(path.read_text()))
    else:
        config = Config()
    config.check_transport()
    return config


def save_config(directory: Path, config: Config) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = config.model_dump(mode="json")
    data["token"] = config.token.get_secret_value()
    temp = directory / "config.new"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, directory / "config.json")
