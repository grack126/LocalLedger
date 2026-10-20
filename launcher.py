#!/usr/bin/env python3
"""Cross-platform launcher for LocalLedger OCR (called by run.bat / run.sh).

Runs inside the project's virtual environment and:
  1. checks the Python version
  2. installs requirements.txt — only when it changed since the last install
  3. generates sample data on the first run
  4. reports optional engines (Tesseract OCR)
  5. starts Streamlit on a free port and opens the browser

Options:
  --port N        preferred port (default 8501; the next free one is used if taken)
  --no-browser    don't open a browser window (headless)
  --reinstall     force reinstalling the requirements
  --check         install + verify everything imports, then exit (no server)
"""
from __future__ import annotations

import argparse
import hashlib
import os
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQUIREMENTS = ROOT / "requirements.txt"
MIN_PYTHON = (3, 11)
STAMP = Path(sys.prefix) / ".localledger-requirements.sha256"


def say(message: str = "") -> None:
    print(f"  {message}" if message else "", flush=True)


def in_virtualenv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def requirements_hash() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def ensure_requirements(force: bool = False) -> None:
    wanted = requirements_hash()
    if not force and STAMP.exists() and STAMP.read_text(encoding="utf-8").strip() == wanted:
        return
    say("Installing dependencies - the first run takes a few minutes...")
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQUIREMENTS)]
    if subprocess.call(cmd, cwd=ROOT) != 0:
        raise SystemExit("\n  Installing dependencies failed - check your internet connection and the "
                         "messages above, then run again.")
    STAMP.write_text(wanted, encoding="utf-8")
    say("Dependencies installed.")


def ensure_sample_data() -> None:
    if (ROOT / "sample_data" / "invoices").is_dir():
        return
    say("Creating sample invoices and master lists in sample_data/ ...")
    subprocess.call([sys.executable, str(ROOT / "generate_sample_data.py")], cwd=ROOT)


def check_imports() -> None:
    sys.path.insert(0, str(ROOT))
    import pdfplumber  # noqa: F401
    import plotly  # noqa: F401
    import pymupdf  # noqa: F401
    import rapidfuzz  # noqa: F401
    import streamlit  # noqa: F401

    from localledger.db import Database
    from localledger.extraction import ExtractionPipeline  # noqa: F401

    Database(":memory:")


def engine_report() -> None:
    sys.path.insert(0, str(ROOT))
    try:
        from localledger.extraction.ocr import INSTALL_HINT, find_tesseract
    except Exception:
        return
    import platform

    path = find_tesseract(os.environ.get("TESSERACT_CMD"))
    if path:
        say(f"OCR: Tesseract found ({path})")
    else:
        say("OCR: Tesseract not found - digital PDFs work; scanned ones need it.")
        say(f"     {INSTALL_HINT.get(platform.system(), '')}")


def free_port(preferred: int) -> int:
    for port in range(preferred, preferred + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise SystemExit(f"No free port between {preferred} and {preferred + 49}.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start LocalLedger OCR")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--reinstall", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    say()
    say("LocalLedger OCR")
    say("---------------")
    if sys.version_info < MIN_PYTHON:
        raise SystemExit(f"  Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required (this is "
                         f"{sys.version.split()[0]}).")
    if not in_virtualenv():
        say("Note: not running in a virtual environment - packages go into this Python installation.")

    ensure_requirements(force=args.reinstall)
    ensure_sample_data()
    engine_report()

    if args.check:
        check_imports()
        say("Check passed: all components import correctly.")
        return 0

    port = free_port(args.port)
    url = f"http://localhost:{port}"
    say()
    say(f"Opening {url}  -  keep this window open while you work; press Ctrl+C to stop.")
    say()
    cmd = [sys.executable, "-m", "streamlit", "run", str(ROOT / "app.py"), "--server.port", str(port),
           "--server.address", "localhost", "--browser.gatherUsageStats", "false",
           "--server.headless", "true" if args.no_browser else "false"]
    try:
        return subprocess.call(cmd, cwd=ROOT)
    except KeyboardInterrupt:
        say("Stopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
