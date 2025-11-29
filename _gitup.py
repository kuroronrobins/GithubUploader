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
_remote_url = ""


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
        global _remote_url
        _remote_url = url
        return url
    print(f"[INFO] remote {DEFAULT_REMOTE_NAME} を設定します。")
    url = input("remote URL (例: https://github.com/kuroronrobins/REPO.git): ").strip()
    while not url:
        url = input("remote URL を入力してください: ").strip()
    run_git("remote", "add", DEFAULT_REMOTE_NAME, url)
    set_exe_name_from_remote(url)
    _remote_url = url
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


def build_bootstrap_entry_script() -> Path:
    owner, repo = parse_github_repo(_remote_url) if _remote_url else (None, None)
    owner = owner or "kuroronrobins"
    repo = repo or (_exe_name or "unknown-repo")
    branch = DEFAULT_STABLE_BRANCH
    exe_basename = _exe_name if _exe_name.lower().endswith(".exe") else f"{_exe_name}.exe"
    bootstrap_path = PYINSTALLER_BUILD / "_bootstrap_entry.py"
    PYINSTALLER_BUILD.mkdir(parents=True, exist_ok=True)
    template = """#!/usr/bin/env python3
\"\"\"Auto-generated bootstrap with self-update for the built exe.\"\"\"
import json
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

try:
    import tkinter as tk
    from tkinter import ttk
except Exception:
    tk = None
    ttk = None

GITHUB_OWNER = "{owner}"
GITHUB_REPO = "{repo}"
GITHUB_BRANCH = "{branch}"
EXE_BASENAME = "{exe_basename}"
VERSION_FILE_NAME = "_version_info.json"
HTTP_TIMEOUT = 8


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def version_info_path() -> Path:
    return base_dir() / VERSION_FILE_NAME


def load_local_version() -> str:
    path = version_info_path()
    if not path.exists():
        return "0.0.0"
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        return info.get("stable", info.get("develop", "0.0.0"))
    except Exception:
        return "0.0.0"


def build_raw_url(path: str) -> str:
    return f"https://raw.githubusercontent.com/{{GITHUB_OWNER}}/{{GITHUB_REPO}}/{{GITHUB_BRANCH}}/{{path.lstrip('/') }}"


def fetch_text(url: str) -> Optional[str]:
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return None


def fetch_binary_with_progress(url: str, ui) -> Optional[bytes]:
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:
            total = int(resp.headers.get("Content-Length", "0") or 0)
            chunks = []
            downloaded = 0
            while True:
                buf = resp.read(64 * 1024)
                if not buf:
                    break
                chunks.append(buf)
                downloaded += len(buf)
                if ui:
                    percent = (downloaded / total * 100) if total else 0
                    ui.set_progress(percent, downloaded, total)
            return b"".join(chunks)
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return None


def parse_remote_version(text: str) -> str:
    try:
        info = json.loads(text)
        return info.get("stable", info.get("develop", "0.0.0"))
    except Exception:
        return "0.0.0"


def version_tuple(ver: str) -> tuple:
    parts = []
    for part in ver.split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def has_newer_version(local_ver: str, remote_ver: str) -> bool:
    return version_tuple(remote_ver) > version_tuple(local_ver)


class UpdaterUI:
    def __init__(self):
        if tk is None or ttk is None:
            raise RuntimeError("tkinter unavailable")
        self.root = tk.Tk()
        self.root.title("GitHubSync アップデート")
        self.root.geometry("360x140")
        self.root.resizable(False, False)
        self.label = tk.Label(self.root, text="新しいバージョンを確認しています...", anchor="w", justify="left", padx=10, pady=10)
        self.label.pack(fill="x")
        self.progress = ttk.Progressbar(self.root, orient="horizontal", mode="determinate", length=320, maximum=100)
        self.progress.pack(padx=10, pady=(0, 4))
        self.detail = tk.Label(self.root, text="", anchor="w", justify="left", padx=10)
        self.detail.pack(fill="x")
        self._refresh()

    def set_status(self, text: str) -> None:
        self.label.config(text=text)
        self._refresh()

    def set_progress(self, percent: float, downloaded: int, total: int) -> None:
        self.progress["value"] = max(0, min(100, percent))
        if total > 0:
            self.detail.config(text=f"{{downloaded//1024}}KB / {{total//1024}}KB")
        else:
            self.detail.config(text=f"{{downloaded//1024}}KB")
        self._refresh()

    def close(self) -> None:
        try:
            self.root.destroy()
        except Exception:
            pass

    def _refresh(self) -> None:
        try:
            self.root.update_idletasks()
            self.root.update()
        except Exception:
            pass


def replace_and_restart(downloaded_path: Path, target_path: Path) -> None:
    ps_script = (
        "\\n$src = '{{downloaded_path}}'\\n"
        "$dst = '{{target_path}}'\\n"
        "Start-Sleep -Milliseconds 900\\n"
        "Copy-Item -Path $src -Destination $dst -Force\\n"
        "Start-Process -FilePath $dst\\n"
    )
    subprocess.Popen(
        ["powershell", "-NoProfile", "-Command", ps_script],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    sys.exit(0)


def try_self_update(local_version: str) -> None:
    version_url = build_raw_url(VERSION_FILE_NAME)
    ui = UpdaterUI() if tk else None
    if ui:
        ui.set_status("新しいバージョンを確認しています...")
    text = fetch_text(version_url)
    if not text:
        if ui:
            ui.close()
        return
    remote_version = parse_remote_version(text)
    if not has_newer_version(local_version, remote_version):
        if ui:
            ui.close()
        return
    exe_url = build_raw_url(f"dist/{{EXE_BASENAME}}")
    if ui:
        ui.set_status("新しいバージョンをダウンロードしています...")
    data = fetch_binary_with_progress(exe_url, ui)
    if not data:
        if ui:
            ui.close()
        return
    if ui:
        ui.set_status("更新を適用しています...")
        ui.set_progress(100, len(data), len(data))
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    current_exe = Path(sys.executable).resolve()
    if ui:
        ui.close()
    replace_and_restart(tmp_path, current_exe)


def run_original_main():
    try:
        import main
        if hasattr(main, "main"):
            main.main()
        else:
            print("main.main() が見つかりません。")
    except Exception as exc:
        print(f"main の実行中にエラー: {exc}")


def _entry():
    local_version = load_local_version()
    if getattr(sys, "frozen", False):
        try_self_update(local_version)
    run_original_main()


if __name__ == "__main__":
    _entry()
"""
    content = template.format(owner=owner, repo=repo, branch=branch, exe_basename=exe_basename)
    bootstrap_path.write_text(content, encoding="utf-8")
    return bootstrap_path

def build_exe(additional_opts: Optional[List[str]] = None) -> bool:
    opts = PYINSTALLER_OPTS.copy()
    if additional_opts:
        opts.extend(additional_opts)
    data_sep = ";" if os.name == "nt" else ":"
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
            "--add-data",
            f"{version_file_path()}{data_sep}.",
        ]
    )
    bootstrap_entry = build_bootstrap_entry_script()
    PYINSTALLER_BUILD.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    cmd = ["pyinstaller", *opts, str(bootstrap_entry)]
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


def list_remote_commits(branch: str, limit: int = 15) -> List[Tuple[str, str, str]]:
    """Return a list of (sha, date, message) for the remote branch."""
    cp = run_git(
        "log",
        f"{DEFAULT_REMOTE_NAME}/{branch}",
        f"-{limit}",
        "--date=format:%Y-%m-%d %H:%M",
        "--pretty=format:%h|%ad|%s",
    )
    commits: List[Tuple[str, str, str]] = []
    for line in cp.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("|", 2)
        if len(parts) == 3:
            commits.append((parts[0], parts[1], parts[2]))
    return commits


def pull_latest(branch: str) -> None:
    if not ensure_branch(branch):
        return
    cp = run_git("pull", DEFAULT_REMOTE_NAME, branch)
    if cp.returncode != 0:
        print(cp.stderr)
    else:
        print(f"[INFO] {branch} を最新状態にしました。")


def checkout_past_commit(branch: str) -> None:
    commits = list_remote_commits(branch)
    if not commits:
        print(f"[WARN] {branch} のコミット履歴が取得できませんでした。")
        return
    print(f"[INFO] {branch} の過去コミット一覧 (最新 {len(commits)} 件)")
    for idx, (sha, date, msg) in enumerate(commits, 1):
        print(f"[{idx:02}] {date} {sha} {msg}")
    sel = input(f"番号を入力 (1-{len(commits)} / Enter で中止): ").strip()
    if not sel.isdigit():
        print("[INFO] 中止しました。")
        return
    num = int(sel)
    if num < 1 or num > len(commits):
        print("[WARN] 範囲外の番号です。")
        return
    sha, date, msg = commits[num - 1]
    print(f"[INFO] {date} {sha} {msg} を checkout --detach します。")
    if count_changes() > 0 and not input_yes_no("未コミットの変更があります。続行しますか?", default=False):
        return
    cp = run_git("checkout", "--detach", sha)
    if cp.returncode != 0:
        print(cp.stderr)
    else:
        print("[INFO] 過去バージョンをチェックアウトしました。")


def pull_flow() -> None:
    branch = select_branch_channel()
    print(f"[INFO] {DEFAULT_REMOTE_NAME} から {branch} をフェッチします。")
    cp = run_git("fetch", DEFAULT_REMOTE_NAME, branch)
    if cp.returncode != 0:
        print(cp.stderr)
        return
    print("[1] 最新を pull")
    print("[2] 過去コミットを一覧から選択")
    choice = input("番号を入力 (1): ").strip() or "1"
    if choice == "2":
        checkout_past_commit(branch)
    else:
        pull_latest(branch)


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
