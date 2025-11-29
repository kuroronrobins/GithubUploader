#!/usr/bin/env python3
# -*- coding:utf-8 -*-
"""
Dummy entry point meant solely for exe creation. It reports the app version
read from version_info.json and exits.
"""

from pathlib import Path
import json
import sys


def version_info_path() -> Path:
    return Path(__file__).resolve().parent / "version_info.json"


def load_app_version() -> str:
    path = version_info_path()
    if not path.exists():
        return "0.0.0"
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        return info.get("stable", info.get("develop", "0.0.0"))
    except Exception:
        return "0.0.0"


def main() -> None:
    version = load_app_version()
    print(f"GitHubSync exe stub version {version}")
    sys.exit(0)


if __name__ == "__main__":
    main()
