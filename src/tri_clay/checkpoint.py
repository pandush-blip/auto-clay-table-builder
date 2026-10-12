"""Run checkpoints so crashes and session expiry can resume."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Checkpoint:
    path: Path
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Checkpoint":
        if path.exists():
            return cls(path, json.loads(path.read_text(encoding="utf-8")))
        return cls(path, {"workbook_id": None, "tables": {}, "exports": {}})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1), encoding="utf-8")

    def get_table(self, key: str) -> dict[str, Any] | None:
        return (self.data.get("tables") or {}).get(key)

    def set_table(self, key: str, value: dict[str, Any]) -> None:
        self.data.setdefault("tables", {})[key] = value
        self.save()

    def clear_table(self, key: str) -> None:
        """Forget one table so a retry builds a fresh one (used on transient failures)."""
        tables = self.data.get("tables") or {}
        if key in tables:
            tables.pop(key)
            self.save()
