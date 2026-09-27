"""Where the app keeps its files (see BLUEPRINT §5)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def default_data_root() -> Path:
    env = os.environ.get("RENTAL_TRACKER_DATA")
    if env:
        return Path(env).expanduser()
    documents = Path.home() / "Documents"
    base = documents if documents.is_dir() else Path.home()
    return base / "RentalTracker"


@dataclass(frozen=True)
class DataDir:
    root: Path

    @property
    def db(self) -> Path:
        return self.root / "rental.db"

    @property
    def documents(self) -> Path:
        return self.root / "documents"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def lock(self) -> Path:
        return self.root / "app.lock"

    def ensure(self) -> DataDir:
        for d in (self.root, self.backups, self.logs):
            d.mkdir(parents=True, exist_ok=True)
        return self
