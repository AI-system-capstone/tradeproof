"""Registry settings; runtime data stays inside this module by default."""
import os
from dataclasses import dataclass, field
from pathlib import Path
from common import contracts as C

ROOT = Path(__file__).resolve().parent

@dataclass
class Settings:
    storage: Path = field(default_factory=lambda: Path(os.getenv("REGISTRY_DATA_DIR", str(ROOT / "data"))))
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", ""))
    trust_key: Path = field(default_factory=lambda: Path(os.getenv("TRUST_KEY", str(ROOT / "trust" / "publisher.pub"))))
    review_threshold: float = 0.20
    max_zip_bytes: int = C.MAX_UPLOAD_BYTES
    max_file_bytes: int = 16 * 1024 * 1024
    max_total_bytes: int = 32 * 1024 * 1024
    max_files: int = 32
    max_ratio: float = 100.0

    def __post_init__(self):
        self.storage = Path(self.storage)
        if not self.database_url:
            self.database_url = "sqlite:///" + (self.storage / "registry.db").as_posix()
        if self.database_url.startswith("postgresql://"):
            self.database_url = self.database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        if not 0 <= self.review_threshold <= 1:
            raise ValueError("Invalid change threshold")
