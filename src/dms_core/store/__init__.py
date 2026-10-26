"""Хранилище: SQLite, дедуп одинаковых текстов (план §4, docs/CONTRACTS.md §4)."""
from .db import KitStore, default_path

__all__ = ["KitStore", "default_path"]
