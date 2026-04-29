#!/usr/bin/env python3
# -*- coding:utf-8 -*-
"""
Minimal entry stub for the packaged executable.
Reports the stored app version and exits after a keypress.
"""

from pathlib import Path
import json
import sys


APP_DATA_FILE = "_app_data.json"


def app_data_path() -> Path:
    return Path(__file__).resolve().parent / APP_DATA_FILE


def load_app_version() -> str:
    path = app_data_path()
    if not path.exists():
        return "0.0.0"
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        version = info.get("version", {})
        if isinstance(version, dict):
            return str(version.get("stable") or version.get("develop") or "0.0.0")
        if isinstance(version, str):
            return version
    except Exception:
        return "0.0.0"
    return "0.0.0"


def main() -> None:
    version = load_app_version()
    print(f"GitHubSync stub version {version}")
    try:
        input("Press Enter to exit...")
    except EOFError:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
