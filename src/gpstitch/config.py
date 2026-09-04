"""Configuration settings for GPStitch."""

import tempfile
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings with environment variable support."""

    model_config = SettingsConfigDict(
        env_prefix="GPSTITCH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server settings
    host: str = "0.0.0.0"
    port: int = 8000

    # Local mode - work with local file paths instead of uploading (default: enabled)
    # Set GPSTITCH_LOCAL_MODE=false to disable and use file upload mode
    local_mode: bool = True

    # Runtime patching for gopro_overlay library
    # Set GPSTITCH_ENABLE_GOPRO_PATCHES=false to disable patches
    enable_gopro_patches: bool = True

    # Use wrapper script for gopro-dashboard.py (enables patches in subprocess)
    # Set GPSTITCH_USE_WRAPPER_SCRIPT=false to use original script
    use_wrapper_script: bool = True

    # File storage settings
    temp_dir: Path = Path(tempfile.gettempdir()) / "gpstitch"
    file_ttl_seconds: int = 3600  # 1 hour
    max_upload_size_bytes: int = 2 * 1024 * 1024 * 1024  # 2GB

    # Template storage directory
    templates_dir: Path = Path.home() / ".gpstitch" / "templates"

    # gopro-overlay config directory (holds user-defined ffmpeg-profiles.json, etc.)
    # Defaults to ~/.gopro-graphics — the same location gopro-dashboard.py reads from.
    gopro_config_dir: Path = Path.home() / ".gopro-graphics"

    # Place name overlay (reverse geocoding)
    # Nominatim's usage policy requires an identifying User-Agent and max 1 req/s.
    place_user_agent: str = "GPStitch (+https://github.com/Romancha/GPStitch)"
    place_min_interval_s: float = 1.0
    place_request_timeout_s: float = 5.0
    place_enable_network: bool = True
    # Progressive refinement: start coarse, bisect only where names differ.
    place_initial_samples: int = 11
    place_target_metres: int = 500
    place_max_lookups: int = 400  # cap an all-different track
    place_preview_max_lookups: int = 25  # preview blocks, so keep its budget small

    # Allowed file extensions
    allowed_extensions: set[str] = {".mp4", ".mov", ".gpx", ".fit", ".srt"}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Ensure directories exist
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.templates_dir.mkdir(parents=True, exist_ok=True)


# Global settings instance
settings = Settings()
