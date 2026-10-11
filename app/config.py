"""Configuration shared by HTTP and file processing layers."""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("GEO_DATA_DIR", "data")))
    database_url: str | None = field(
        default_factory=lambda: os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL"), repr=False
    )
    vercel: bool = field(default_factory=lambda: os.getenv("VERCEL") == "1")
    storage_mode: str = field(default_factory=lambda: os.getenv("GEO_STORAGE_MODE", "auto"))
    max_upload_bytes: int = 20 * 1024 * 1024
    max_uncompressed_bytes: int = 100 * 1024 * 1024
    max_archive_entries: int = 100
    max_features: int = 10_000
    max_coordinates: int = 500_000

    @property
    def upload_limit(self) -> int:
        # Leave room for multipart overhead below Vercel's function request limit.
        return min(self.max_upload_bytes, 3 * 1024 * 1024) if self.vercel else self.max_upload_bytes

    @property
    def storage_backend(self) -> str:
        if self.storage_mode not in {"auto", "none", "sqlite", "postgres"}:
            raise ValueError("GEO_STORAGE_MODE must be auto, none, sqlite, or postgres.")
        if self.storage_mode != "auto":
            return self.storage_mode
        if self.database_url:
            return "postgres"
        return "none" if self.vercel else "sqlite"

    @property
    def result_limit(self) -> int | None:
        # Bound the actual JSON response, including map geometry and attributes.
        return 4 * 1024 * 1024 if self.vercel else None


class FileError(Exception):
    """A client supplied file cannot be processed."""

    def __init__(self, message: str, status_code: int = 422):
        self.message = message
        self.status_code = status_code
        super().__init__(message)
