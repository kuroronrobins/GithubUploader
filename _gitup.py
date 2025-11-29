#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHubSync utility:
- Pull/push with stable/develop branches
- GPT commit summary (除外パス対応)
- PyInstaller ビルド（exe 名は _version_info.json exe_name > remote repo 名 > main）
"""

import argparse
import base64
import datetime
import getpass
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    requests = None

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

APP_NAME = "GitHubSync"
APP_VERSION = "v1.0.0"
DEFAULT_REMOTE_NAME = "origin"
REMOTE_BASE_URL = "https://github.com/kuroronrobins"
DEFAULT_STABLE_BRANCH = "stable"
DEFAULT_DEV_BRANCH = "develop"
AUTO_COMMIT_PREFIX = "chore:"
PYINSTALLER_OPTS = ["--onefile", "--clean"]
PYINSTALLER_DIST = Path("dist")
PYINSTALLER_BUILD = Path("build")
EXCLUDED_PATHS = {"_excluded_paths.json"}
VERSION_FILE = "_version_info.json"

ENCODED_OPENAI_API_KEY = (
    "c2stcHJvai13Q09HbnZOTFJ2MVE3VmU0UGFoU3VHU180M0xBUk9FRERTLTlBeExGM0hDRjZYN1NybURRV0sy"
    "bW90LVp3b3ZmY25DQ3Q4VERnSVQzQmxia0ZKSUY1cHd1c1pzUFEyQlFQNzlNaVJpbWxKUlY5c3VmUFBKZDZCdDhLdFpfRzlFSFBhdGQ0SVlRWGZCYkdRdVVJV2pPY2lnSUtXa0E="
)

OPENAI_MODEL = "gpt-5-nano"
OPENAI_MAX_TOKENS = 2048
OPENAI_REASONING_EFFORT_PRIMARY = "medium"

_openai_client: Optional["OpenAI"] = None
_exe_name = "main"


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def version_file_path() -> Path:
    return base_dir() / VERSION_FILE


def load_version_info() -> Dict[str, str]:
    path = version_file_path()
    if not path.exists():
        return {"stable": "0.0.0", "develop": "0.0.0", "exe_name": ""}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"stable": "0.0.0", "develop": "0.0.0", "exe_name": ""}


def save_version_info(data: Dict[str, str]) -> None:
    version_file_path().write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def ensure_version_file() -> None:
    if not version_file_path().exists():
        save_version_info({"stable": "0.0.0", "develop": "0.0.0", "exe_name": ""})


def local_version(channel: str) -> str:
    info = load_version_info()
    return info.get(channel, "0.0.0")


def update_local_version(channel: str, version: str) -> None:
    data = load_version_info()
    data[channel] = version
    save_version_info(data)


def decode_openai_key() -> Optional[str]:
    if not ENCODED_OPENAI_API_KEY:
        return None
    try:
        return base64.b64decode(ENCODED_OPENAI_API_KEY).decode("utf-8")
    except Exception:
        return None


def get_api_key() -> Optional[str]:
    key = decode_openai_key()
    if key:
        return key
    return os.environ.get("OPENAI_API_KEY")


def get_openai_client() -> Optional["OpenAI"]:
    global _openai_client
    if OpenAI is None:
        print("[WARN] openai がインストールされていません。pip install openai")
        return None
    if _openai_client is not None:
        return _openai_client
    api_key = get_api_key()
    if not api_key:
        print("[WARN] OpenAI API キーが未設定です。")
        return None
    _openai_client = OpenAI(api_key=api_key)
    return _openai_client


def run_git(*args: str, check: bool = False, capture_output: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git", *args]
    return subprocess.run(cmd, check=check, capture_output=capture_output, text=True, encoding="utf-8", errors="replace")


def ensure_git_identity() -> None:
    def cfg(key: str) -> str:
        cp = run_git("config", "--get", key)
        return cp.stdout.strip() if cp.returncode == 0 else ""

    name = cfg("user.name")
    email = cfg("user.email")
    if name and email:
        return
    print("[INFO] Git user.name / user.email を設定します。")
    if not name:
        val = input("user.name: ").strip()
        if val:
            run_git("config", "user.name", val)
    if not email:
        val = input("user.email: ").strip()
        if val:
            run_git("config", "user.email", val)


def setup_repository() -> None:
    print("[INFO] Git リポジトリがありません。init を実行します。")
    run_git("init")


def ensure_git_repo() -> None:
    cp = run_git("rev-parse", "--is-inside-work-tree")
    if cp.returncode != 0 or cp.stdout.strip() != "true":
        setup_repository()


def parse_github_repo(remote_url: str) -> Optional[Tuple[str, str]]:
    if remote_url.startswith("git@github.com:"):
        slug = remote_url.split(":", 1)[1]
    elif remote_url.startswith("https://github.com/"):
        slug = remote_url.split("https://github.com/", 1)[1]
    else:
        return None
    slug = slug.removesuffix(".git")
    if "/" not in slug:
        return None
    owner, repo = slug.split("/", 1)
    return owner, repo


def set_exe_name_from_remote(remote_url: str) -> None:
    info = load_version_info()
    name = info.get("exe_name", "").strip()
    if name:
        set_exe_name(name)
        return
    repo = parse_github_repo(remote_url)
    if repo:
        set_exe_name(repo[1])


def set_exe_name(name: str) -> None:
    global _exe_name
    _exe_name = name or _exe_name


def ensure_remote() -> str:
    cp = run_git("remote", "get-url", DEFAULT_REMOTE_NAME)
    if cp.returncode == 0:
        url = cp.stdout.strip()
        set_exe_name_from_remote(url)
        return url
    print(f"[INFO] remote {DEFAULT_REMOTE_NAME} を設定します。")
    url = input("remote URL (例: https://github.com/kuroronrobins/REPO.git): ").strip()
    while not url:
        url = input("remote URL を入力してください: ").strip()
    run_git("remote", "add", DEFAULT_REMOTE_NAME, url)
    set_exe_name_from_remote(url)
    return url


def get_current_branch() -> str:
    cp = run_git("rev-parse", "--abbrev-ref", "HEAD")
    return cp.stdout.strip() or "(unknown)"


def ensure_branch(branch: str) -> bool:
    cp = run_git("rev-parse", "--verify", branch)
    if cp.returncode == 0:
        run_git("checkout", branch)
        return True
    cp = run_git("checkout", "-b", branch)
    if cp.returncode != 0:
        print(cp.stderr)
        return False
    return True


def input_with_default(message: str, default: str) -> str:
    val = input(f"{message} [{default}]: ").strip()
    return val or default


def select_branch_channel() -> str:
    choice = input("[1] Stable / [2] Development (1): ").strip()
    return DEFAULT_DEV_BRANCH if choice == "2" else DEFAULT_STABLE_BRANCH


def stage_all_changes() -> bool:
    return run_git("add", "-A").returncode == 0


def count_changes() -> int:
    cp = run_git("status", "--porcelain")
    return len([ln for ln in cp.stdout.splitlines() if ln.strip()])


def is_excluded(path: str) -> bool:
    normalized = path.replace("\\", "/")
    for pat in EXCLUDED_PATHS:
        pat_norm = pat.rstrip("/").replace("\\", "/")
        if not pat_norm:
            continue
        if normalized == pat_norm or normalized.startswith(pat_norm + "/"):
            return True
    return False


def make_diff_summary() -> Tuple[str, str]:
    cp = run_git("diff", "--cached", "--numstat")
    lines = [ln for ln in cp.stdout.splitlines() if ln.strip()]
    if not lines:
        return "変更なし", ""
    total_add = total_del = 0
    entries = []
    for ln in lines:
        parts = ln.split("\t")
        if len(parts) >= 3:
            add = int(parts[0]) if parts[0].isdigit() else 0
            delete = int(parts[1]) if parts[1].isdigit() else 0
            path = parts[2]
            if is_excluded(path):
                continue
            total_add += add
            total_del += delete
            entries.append(f"{path}: +{add}/-{delete}")
    if not entries:
        return "変更なし(除外のみ)", ""
    summary = f"{len(entries)} ファイル (+{total_add}/-{total_del})"
    return summary, "\n".join(entries)


def call_openai_for_summary(diff_text: str, fallback_summary: str) -> Tuple[str, str]:
    client = get_openai_client()
    if client is None:
        return fallback_summary, ""
    system_prompt = (
        "Git の変更を60-140文字の日本語一行コミットメッセージにまとめてください。"
        " 目的(修正/機能/設定等)と主な変更箇所を明確に。"
    )
    user_prompt = textwrap.dedent(
        f"""
        以下の diff を要約してコミットメッセージを1行で出力してください。

        --- diff ---
        {diff_text}
        --- end ---
        """
    ).strip()
    try:
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            max_completion_tokens=OPENAI_MAX_TOKENS,
            reasoning_effort=OPENAI_REASONING_EFFORT_PRIMARY,
        )
        text = extract_openai_text(completion)
        usage = format_openai_usage(getattr(completion, "usage", None))
        return text or fallback_summary, usage
    except Exception as exc:
        print(f"[WARN] OpenAI 呼び出しに失敗: {exc}")
        return fallback_summary, ""


def extract_openai_text(response) -> str:
    if not response or not hasattr(response, "choices"):
        return ""
    choice = response.choices[0]
    content = getattr(choice.message, "content", None)
    if isinstance(content, str):
        return " ".join(content.split())
    if isinstance(content, list):
        return " ".join(" ".join(part.get("text", "") for part in content if isinstance(part, dict)).split())
    return ""


def format_openai_usage(usage) -> str:
    if not usage:
        return ""
    parts = []
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        val = getattr(usage, name, None)
        if val is not None:
            parts.append(f"{name.replace('_', ' ')} {val}")
    return "OpenAI usage: " + " / ".join(parts) if parts else ""


def require_stable_password() -> bool:
    while True:
        pwd = getpass.getpass("Stable push 用パスワードを入力してください: ").strip()
        if not pwd:
            print("[WARN] 未入力です。再試行します。")
            continue
        if pwd == "kurokawa":
            print("[INFO] パスワードOK。")
            return True
        retry = input("[ERROR] パスワードが違います。再入力しますか? [Y/n]: ").strip().lower()
        if retry in ("n", "no"):
            print("[INFO] Stable push を中止します。")
            return False


def build_exe(additional_opts: Optional[List[str]] = None) -> bool:
    opts = PYINSTALLER_OPTS.copy()
    if additional_opts:
        opts.extend(additional_opts)
    opts.extend(
        [
            "--distpath",
            str(PYINSTALLER_DIST),
            "--workpath",
            str(PYINSTALLER_BUILD),
            "--specpath",
            str(PYINSTALLER_BUILD),
            "--name",
            _exe_name,
        ]
    )
    PYINSTALLER_BUILD.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    cmd = ["pyinstaller", *opts, "main.py"]
    print("[INFO] " + " ".join(cmd))
    cp = subprocess.run(cmd, cwd=base_dir(), env=env)
    return cp.returncode == 0


def push_flow() -> None:
    channel = select_branch_channel()
    if not ensure_branch(channel):
        return
    if count_changes() == 0:
        print("[INFO] 変更がありません。")
        return

    if input_yes_no("先に PyInstaller で exe を作成しますか?", default=True):
        extra = input("追加オプション (Enter 〜なし): ").strip().split()
        extra = [opt for opt in extra if opt]
        if not build_exe(extra):
            print("[ERROR] PyInstaller ビルドに失敗しました。")
            return

    if not stage_all_changes():
        print("[ERROR] git add に失敗しました。")
        return

    commit_summary, diff_text = make_diff_summary()
    summary_text, usage_text = call_openai_for_summary(diff_text, commit_summary)
    commit_msg = f"{AUTO_COMMIT_PREFIX} {datetime.datetime.now():%Y-%m-%d %H:%M} - {summary_text}"
    if usage_text:
        print(usage_text)
    print(f"Commit message:\n{commit_msg}")
    if not input_yes_no("このメッセージでコミットしますか?", default=False):
        return
    cp = run_git("commit", "-m", commit_msg)
    if cp.returncode != 0:
        print(cp.stderr)
        return
    version_input = input_with_default(f"{channel} のバージョン (現在 {local_version(channel)}): ", local_version(channel))
    update_local_version(channel, version_input)
    if channel == DEFAULT_STABLE_BRANCH and not require_stable_password():
        return
    ensure_branch(channel)
    push_cmd = ["push"] if has_upstream() else ["push", "--set-upstream", DEFAULT_REMOTE_NAME, channel]
    cp = run_git(*push_cmd)
    if cp.returncode != 0:
        print(cp.stderr)
    else:
        print("[INFO] Push 完了しました。")


def input_yes_no(message: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    ans = input(f"{message} {suffix}: ").strip().lower()
    if not ans:
        return default
    return ans in ("y", "yes")


def has_upstream() -> bool:
    cp = run_git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    return cp.returncode == 0


def parse_args():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--skip-update-check", action="store_true")
    parser.add_argument("--apply-update", type=str)
    parser.add_argument("--new-version", type=str)
    return parser.parse_known_args()


def main_menu() -> None:
    while True:
        os.system("cls" if os.name == "nt" else "clear")
        print("======================================")
        print(f"{APP_NAME} {APP_VERSION}")
        print("======================================")
        print(f"Branch: {get_current_branch()}")
        print(f"Exe name: {_exe_name}")
        print(f"Stable version: {local_version(DEFAULT_STABLE_BRANCH)}")
        print()
        print("[1] Pull")
        print("[2] Push")
        print("[3] Exit")
        sel = input("番号を入力: ").strip()
        if sel == "1":
            pull_flow()
        elif sel == "2":
            push_flow()
        elif sel == "3":
            break
        else:
            print("1-3 を入力してください。")
        input("Enter で続行...")


def main():
    args, _ = parse_args()
    ensure_version_file()
    ensure_git_repo()
    ensure_git_identity()
    ensure_remote()
    main_menu()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INFO] 終了します。")
