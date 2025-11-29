#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHubSync main entry point (also reused by _Gitupload).
"""

import argparse
import base64
import datetime
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
except ImportError:  # pragma: no cover
    requests = None

try:
    from openai import OpenAI  # pip install openai
except ImportError:  # pragma: no cover
    OpenAI = None

APP_NAME = "GitHubSync"
APP_VERSION = "v1.0.0"
DEFAULT_REMOTE_NAME = "origin"
REMOTE_BASE_URL = "https://github.com/kuroronrobins"
DEFAULT_REMOTE_URL = f"{REMOTE_BASE_URL}/ExcelBatchReplace.git"
DEFAULT_STABLE_BRANCH = "stable"
DEFAULT_DEV_BRANCH = "develop"
AUTO_COMMIT_PREFIX = "chore:"
PYINSTALLER_DEFAULT_OPTS = ["--onefile", "--clean"]
PYINSTALLER_DIST_DIR = Path("dist")
PYINSTALLER_BUILD_DIR = Path("build")

# Base64 encoded key to avoid storing raw API key inline. It decodes inside the script so no env var is required.
ENCODED_OPENAI_API_KEY = (
    "c2stcHJvai13Q09HbnZOTFJ2MVE3VmU0UGFoU3VHU180M0xBUk9FRERTLTlBeExGM0hDRjZYN1NybURRV0sy"
    "bW90LVp3b3ZmY25DQ3Q4VERnSVQzQmxia0ZKSUY1cHd1c1pzUFEyQlFQNzlNaVJpbWxKUlY5c3VmUFBKZDZCdDhLdFpfRzlFSFBhdGQ0SVlRWGZCYkdRdVVJV2pPY2lnSUtXa0E="
)

OPENAI_MODEL = "gpt-5-nano"
OPENAI_MAX_TOKENS = 2048
OPENAI_REASONING_EFFORT_PRIMARY = "medium"
OPENAI_REASONING_EFFORT_FALLBACK = "medium"

_openai_client: Optional["OpenAI"] = None


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def version_file_path() -> Path:
    return base_dir() / "version_info.json"


def load_version_info() -> Dict[str, str]:
    path = version_file_path()
    if not path.exists():
        return {"stable": "0.0.0", "develop": "0.0.0"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"stable": "0.0.0", "develop": "0.0.0"}


def local_version(channel: str) -> str:
    info = load_version_info()
    return info.get(channel, info.get("stable", "0.0.0"))


def save_version_info(data: Dict[str, str]) -> None:
    version_file_path().write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def update_local_version(channel: str, version: str) -> None:
    data = load_version_info()
    data[channel] = version
    save_version_info(data)


def ensure_version_file() -> None:
    if not version_file_path().exists():
        save_version_info({"stable": "0.0.0", "develop": "0.0.0"})


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
        print("[WARN] openai パッケージがありません。pip install openai してください。")
        return None
    if _openai_client is not None:
        return _openai_client
    api_key = get_api_key()
    if not api_key:
        print("[WARN] OpenAI API キーが見つかりません。")
        return None
    _openai_client = OpenAI(api_key=api_key)
    return _openai_client


def run_git(*args: str, check: bool = False, capture_output: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git", *args]
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture_output,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def ensure_git_identity() -> None:
    def get_config(key: str) -> str:
        cp = run_git("config", "--get", key)
        if cp.returncode != 0:
            return ""
        return cp.stdout.strip()

    name = get_config("user.name")
    email = get_config("user.email")
    if name and email:
        return
    print("[INFO] Git user.name/user.email が未設定です。設定をお願いします。")
    if not name:
        new_name = input("user.name を入力: ").strip()
        if new_name:
            run_git("config", "user.name", new_name)
    if not email:
        new_email = input("user.email を入力: ").strip()
        if new_email:
            run_git("config", "user.email", new_email)


def setup_repository() -> None:
    print("[INFO] Git リポジトリが見つかりませんでした。セットアップします。")
    print(" 1) kuroronrobins にある既存リポジトリを指定")
    print(" 2) 新規リポジトリとして初期化")
    choice = input("番号を入力してください (1/2): ").strip()
    run_git("init")
    if choice == "1":
        repo_name = input("利用するリポジトリ名 (例: ExcelBatchReplace): ").strip() or "ExcelBatchReplace"
        remote_url = f"{REMOTE_BASE_URL}/{repo_name}.git"
        if input_yes_no(f"{remote_url} を origin として登録しますか?", default=True):
            run_git("remote", "add", DEFAULT_REMOTE_NAME, remote_url)
    else:
        repo_name = input("新規リポジトリ名 (例: MyProject): ").strip() or "MyProject"
        remote_url = f"{REMOTE_BASE_URL}/{repo_name}.git"
        if input_yes_no(f"{remote_url} を origin にしますか?", default=True):
            run_git("remote", "add", DEFAULT_REMOTE_NAME, remote_url)
        run_git("add", "-A")
        run_git("commit", "-m", "chore: initial import")
        run_git("checkout", "-b", DEFAULT_DEV_BRANCH)
        run_git("checkout", "-b", DEFAULT_STABLE_BRANCH)
        run_git("checkout", DEFAULT_DEV_BRANCH)
        print("[INFO] develop/stable ブランチを作成しました。")


def ensure_git_repo() -> None:
    cp = run_git("rev-parse", "--is-inside-work-tree")
    if cp.returncode != 0 or cp.stdout.strip() != "true":
        setup_repository()


def ensure_remote() -> str:
    cp = run_git("remote", "get-url", DEFAULT_REMOTE_NAME)
    if cp.returncode == 0:
        return cp.stdout.strip()
    print(f"[INFO] remote {DEFAULT_REMOTE_NAME} を設定します。")
    remote_url = input("remote URL: ").strip() or DEFAULT_REMOTE_URL
    run_git("remote", "add", DEFAULT_REMOTE_NAME, remote_url)
    return remote_url


def get_current_branch() -> str:
    cp = run_git("rev-parse", "--abbrev-ref", "HEAD")
    branch = cp.stdout.strip()
    return branch or "(unknown)"


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


def select_branch_channel() -> str:
    print("[1] Stable mode")
    print("[2] Development mode")
    sel = input("番号を入力 (デフォルト 1): ").strip()
    return DEFAULT_DEV_BRANCH if sel == "2" else DEFAULT_STABLE_BRANCH


def select_pull_branch() -> str:
    print("[1] Stable")
    print("[2] Development")
    print("[3] 最新 (stable/develop のどちらか最新) ")
    sel = input("番号 (デフォルト 1): ").strip()
    if sel == "2":
        return DEFAULT_DEV_BRANCH
    if sel == "3":
        return resolve_latest_remote_branch()
    return DEFAULT_STABLE_BRANCH


def fetch_remote_branch(branch: str) -> None:
    run_git("fetch", DEFAULT_REMOTE_NAME, branch)


def resolve_latest_remote_branch() -> str:
    fetch_remote_branch(DEFAULT_STABLE_BRANCH)
    fetch_remote_branch(DEFAULT_DEV_BRANCH)
    stable_ts = remote_head_timestamp(DEFAULT_STABLE_BRANCH)
    dev_ts = remote_head_timestamp(DEFAULT_DEV_BRANCH)
    return DEFAULT_DEV_BRANCH if dev_ts > stable_ts else DEFAULT_STABLE_BRANCH


def remote_head_timestamp(branch: str) -> int:
    cp = run_git("show", "-s", "--format=%ct", f"{DEFAULT_REMOTE_NAME}/{branch}")
    try:
        return int(cp.stdout.strip())
    except Exception:
        return 0


def display_branch_history(branch: str) -> List[str]:
    cp = run_git("log", f"{DEFAULT_REMOTE_NAME}/{branch}", "--pretty=format:%h|%ci|%s", "-n", "10")
    lines = [ln for ln in cp.stdout.splitlines() if ln.strip()]
    for idx, ln in enumerate(lines, 1):
        short, dt, msg = ln.split("|", 2)
        print(f"{idx}) {dt} {short} {msg}")
    return lines


def prompt_commit_selection(lines: List[str]) -> Optional[str]:
    if not lines:
        return None
    sel = input("反映したいコミット番号 (Enter=最新): ").strip()
    if not sel:
        return lines[0].split("|", 1)[0]
    if not sel.isdigit():
        return None
    idx = int(sel) - 1
    if 0 <= idx < len(lines):
        return lines[idx].split("|", 1)[0]
    return None


def pull_flow() -> None:
    branch = select_pull_branch()
    fetch_remote_branch(branch)
    history = display_branch_history(branch)
    target_commit = prompt_commit_selection(history)
    if not target_commit:
        print("[INFO] コミットが選べませんでした。pull を終了します。")
        return
    if not ensure_branch(branch):
        print("[ERROR] ブランチ切り替えに失敗しました。")
        return
    cp = run_git("reset", "--hard", target_commit)
    if cp.returncode != 0:
        print(cp.stderr)
        return
    print(f"[INFO] {branch} を {target_commit} にリセットしました。")
    run_git("clean", "-fd")


def stage_all_changes() -> bool:
    cp = run_git("add", "-A")
    return cp.returncode == 0


def count_changes() -> int:
    cp = run_git("status", "--porcelain")
    return len([ln for ln in cp.stdout.splitlines() if ln.strip()])


def make_diff_summary() -> Tuple[str, str]:
    fallback = "変更点がありません。"
    cp = run_git("diff", "--cached", "--numstat")
    lines = [ln for ln in cp.stdout.splitlines() if ln.strip()]
    if not lines:
        return fallback, ""
    total_add = total_del = 0
    entries = []
    for ln in lines:
        parts = ln.split("\t")
        if len(parts) >= 3:
            add = int(parts[0]) if parts[0].isdigit() else 0
            delete = int(parts[1]) if parts[1].isdigit() else 0
            path = parts[2]
            total_add += add
            total_del += delete
            entries.append(f"{path}: +{add}/-{delete}")
    summary = f"{len(entries)} ファイル (+{total_add}/-{total_del})"
    diff_text = "\n".join(entries)
    return summary, diff_text


def call_openai_for_summary(diff_text: str, fallback_summary: str) -> Tuple[str, str]:
    client = get_openai_client()
    if client is None:
        return fallback_summary, ""
    system_prompt = (
        "You summarize git changes into a Japanese single-line commit message between 60-140 characters, "
        "mentioning modules/files when possible and clarifying purpose (bug fix, feature, refactor, config, etc.)."
    )
    user_prompt = textwrap.dedent(
        f"""
        以下の diff を基にコミットメッセージを作成してください。

        --- diff ---
        {diff_text}
        --- end ---
        """
    ).strip()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    payload = {
        "model": OPENAI_MODEL,
        "messages": messages,
        "max_completion_tokens": OPENAI_MAX_TOKENS,
        "reasoning_effort": OPENAI_REASONING_EFFORT_PRIMARY,
    }
    try:
        completion = client.chat.completions.create(**payload)
        text = extract_openai_text(completion)
        usage = format_openai_usage(getattr(completion, "usage", None))
        return (text or fallback_summary, usage)
    except Exception as exc:
        print(f"[WARN] OpenAI 呼び出しに失敗しました: {exc}")
        return fallback_summary, ""


def extract_openai_text(response) -> str:
    if response is None:
        return ""
    if not hasattr(response, "choices"):
        return ""
    choice = response.choices[0]
    content = getattr(choice.message, "content", None) or getattr(choice, "content", None)
    if isinstance(content, str):
        return " ".join(content.split())
    if isinstance(content, list):
        joined = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return " ".join(joined.split())
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
    pwd = input("Stable channel の push にはパスワードが必要です: ").strip()
    if pwd == "kurokawa":
        return True
    print("[ERROR] パスワードが違います。")
    return False


def build_exe(additional_opts: Optional[List[str]] = None) -> bool:
    opts = PYINSTALLER_DEFAULT_OPTS.copy()
    if additional_opts:
        opts.extend(additional_opts)
    opts.extend(
        [
            "--distpath",
            str(PYINSTALLER_DIST_DIR),
            "--workpath",
            str(PYINSTALLER_BUILD_DIR),
            "--specpath",
            str(PYINSTALLER_BUILD_DIR),
        ]
    )
    PYINSTALLER_BUILD_DIR.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    command = ["pyinstaller", *opts, "main.py"]
    print(f"[INFO] {' '.join(command)}")
    cp = subprocess.run(command, cwd=base_dir(), env=env)
    return cp.returncode == 0


def push_flow() -> None:
    channel = select_branch_channel()
    if not ensure_branch(channel):
        return
    if count_changes() == 0:
        print("[INFO] 変更がありません。")
        return
    if not stage_all_changes():
        print("[ERROR] git add に失敗しました。")
        return
    commit_summary, diff_text = make_diff_summary()
    summary_text, usage_text = call_openai_for_summary(diff_text, commit_summary)
    commit_msg = f"{AUTO_COMMIT_PREFIX} {datetime.datetime.now():%Y-%m-%d %H:%M} - {summary_text}"
    print()
    if usage_text:
        print(usage_text)
    print(f"Commit message: {commit_msg}")
    if not input_yes_no("このメッセージでコミットしますか?", default=False):
        return
    cp = run_git("commit", "-m", commit_msg)
    if cp.returncode != 0:
        print(cp.stderr)
        return
    version_input = input_with_default(
        f"{channel} のバージョン (現在 {local_version(channel)}): ",
        local_version(channel),
    )
    update_local_version(channel, version_input)
    if channel == DEFAULT_STABLE_BRANCH and not require_stable_password():
        return
    if input_yes_no("PyInstaller で exe を作りますか?", default=True):
        extra = input("追加オプション (Enter 〜なし): ").strip().split()
        extra = [opt for opt in extra if opt]
        if not build_exe(extra):
            print("[ERROR] PyInstaller ビルドに失敗しました。")
            return
    ensure_branch(channel)
    push_cmd = ["push"] if has_upstream() else ["push", "--set-upstream", DEFAULT_REMOTE_NAME, channel]
    cp = run_git(*push_cmd)
    if cp.returncode != 0:
        print(cp.stderr)
    else:
        print("[INFO] Push が完了しました。")


def input_yes_no(message: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    ans = input(f"{message} {suffix}: ").strip().lower()
    if not ans:
        return default
    return ans in ("y", "yes")


def has_upstream() -> bool:
    cp = run_git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    return cp.returncode == 0


def parse_github_repo(remote_url: str) -> Optional[Tuple[str, str]]:
    if not remote_url:
        return None
    if remote_url.startswith("git@github.com:"):
        slug = remote_url.split(":", 1)[1]
    elif remote_url.startswith("https://github.com/"):
        slug = remote_url.split("https://github.com/", 1)[1]
    else:
        return None
    slug = slug.removesuffix(".git")
    if "/" not in slug:
        return None
    owner, name = slug.split("/", 1)
    return owner, name


def select_release_asset(release_data: Dict) -> Optional[Dict]:
    assets = release_data.get("assets") or []
    for asset in assets:
        if asset.get("name", "").lower().endswith(".exe"):
            return asset
    return assets[0] if assets else None


def fetch_latest_release(owner: str, repo: str) -> Optional[Dict]:
    if requests is None:
        return None
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    headers = {"User-Agent": APP_NAME}
    try:
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


def normalize_version(v: str) -> List[int]:
    cleaned = "".join(ch for ch in v if ch.isdigit() or ch == ".")
    parts = [int(part) for part in cleaned.split(".") if part.isdigit()]
    return parts or [0]


def compare_versions(v1: str, v2: str) -> int:
    p1 = normalize_version(v1)
    p2 = normalize_version(v2)
    length = max(len(p1), len(p2))
    for i in range(length):
        n1 = p1[i] if i < len(p1) else 0
        n2 = p2[i] if i < len(p2) else 0
        if n1 != n2:
            return 1 if n1 > n2 else -1
    return 0


def download_asset(url: str, target: Path) -> bool:
    if requests is None:
        return False
    try:
        with requests.get(url, stream=True, timeout=10) as resp:
            resp.raise_for_status()
            with target.open("wb") as fp:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        fp.write(chunk)
        return True
    except Exception as exc:
        print(f"[ERROR] アセットのダウンロードに失敗: {exc}")
        return False


def spawn_update_process(update_exe: Path, target_exe: Path, new_version: str) -> None:
    args = [
        str(update_exe),
        "--apply-update",
        str(target_exe),
        "--skip-update-check",
        "--new-version",
        new_version,
    ]
    subprocess.Popen(args, cwd=target_exe.parent)


def maybe_check_for_updates(remote_url: str, skip: bool) -> bool:
    if skip or not getattr(sys, "frozen", False):
        return False
    if not remote_url or requests is None:
        return False
    repo_info = parse_github_repo(remote_url)
    if not repo_info:
        return False
    release = fetch_latest_release(*repo_info)
    if not release:
        return False
    remote_version = release.get("tag_name", "0.0.0").lstrip("v")
    local_ver = local_version(DEFAULT_STABLE_BRANCH)
    if compare_versions(remote_version, local_ver) <= 0:
        return False
    asset = select_release_asset(release)
    if not asset:
        return False
    download_url = asset.get("browser_download_url")
    if not download_url:
        return False
    tmp_exe = Path(tempfile.gettempdir()) / f"{Path(sys.executable).stem}_update_{int(time.time())}.exe"
    if not download_asset(download_url, tmp_exe):
        return False
    print(f"[INFO] 新しい stable バージョン {remote_version} を検出しました。更新します。")
    spawn_update_process(tmp_exe, Path(sys.executable), remote_version)
    sys.exit(0)


def finalize_update(target_path: str, new_version: Optional[str]) -> None:
    own = Path(sys.executable)
    target = Path(target_path)
    attempts = 0
    while attempts < 15:
        try:
            shutil.copy2(own, target)
            break
        except PermissionError:
            time.sleep(1)
            attempts += 1
    else:
        print("[ERROR] 更新用 exe を配置できませんでした。")
        return
    if new_version:
        update_local_version(DEFAULT_STABLE_BRANCH, new_version)
    subprocess.Popen([str(target), "--skip-update-check"])
    sys.exit(0)


def parse_args():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--skip-update-check", action="store_true")
    parser.add_argument("--apply-update", type=str)
    parser.add_argument("--new-version", type=str)
    return parser.parse_known_args()


def main_menu() -> None:
    while True:
        os.system("cls" if os.name == "nt" else "clear")
        branch = get_current_branch()
        print("======================================")
        print(f"{APP_NAME} {APP_VERSION}")
        print("======================================")
        print(f"Branch: {branch}")
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
    if args.apply_update:
        finalize_update(args.apply_update, args.new_version)
    ensure_version_file()
    ensure_git_repo()
    ensure_git_identity()
    remote_url = ensure_remote()
    maybe_check_for_updates(remote_url, args.skip_update_check)
    main_menu()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INFO] 終了します。")
