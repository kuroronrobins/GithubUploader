from pathlib import Path
import json
import sys

def base_dir() -> Path:
    # PyInstaller などで「凍結」されているか判定
    if getattr(sys, "frozen", False):
        # exe ファイルのあるフォルダ
        return Path(sys.executable).resolve().parent
    # 通常のスクリプト実行
    return Path(__file__).resolve().parent

def version_info_path() -> Path:
    return base_dir() / "_version_info.json"

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
