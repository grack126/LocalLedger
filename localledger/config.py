"""Application paths and process-wide constants.

Every path can be overridden with an environment variable so the app can be
pointed at a different data folder (e.g. a synced drive) without code changes.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "LocalLedger OCR"

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent

DATA_DIR = Path(os.environ.get("LOCALLEDGER_DATA_DIR", PROJECT_ROOT / "data")).expanduser()
DB_PATH = Path(os.environ.get("LOCALLEDGER_DB", DATA_DIR / "localledger.db")).expanduser()
INBOX_DIR = DATA_DIR / "inbox"
DEFAULT_ARCHIVE_DIR = DATA_DIR / "Archive"
EXPORT_DIR = DATA_DIR / "exports"

SCHEMA_PATH = PACKAGE_DIR / "schema.sql"
