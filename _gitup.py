#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHubSync CLI:
- Pull/push with stable/develop/latest selection and remote history browsing
- AI-generated commit messages (日本語, 70-120 chars, 含む範囲/主要変更)
- PyInstaller build wrapping bootstrap updater + main.py + config
- Safe config handling with exclude/stage-protected filters
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

APP_NAME_DEFAULT = "GitHubSync"
APP_VERSION = "v3.0.0"
DEFAULT_REMOTE_NAME = "origin"
DEFAULT_STABLE_BRANCH = "stable"
DEFAULT_DEVELOP_BRANCH = "develop"
AUTO_COMMIT_PREFIX = "chore:"
AI_COMMIT_SUMMARY = (
    "ステージ済みの変更点から、追加/修正/削除の概要、影響範囲、更新や削除された機能を含めて日本語で1行にまとめてください。"
)
APP_DATA_FILE = "_app_data.json"
EXCLUDE_DEFAULT = ["build/", "dist/", ".git/", "__pycache__/", ".venv/"]
STAGE_PROTECTED_DEFAULT = ["build/", "dist/", "__pycache__/", "*.pyc", "*.pyd", "*.pyo", "release/latest/*.exe"]
REMOTE_BASE_DEFAULT = "https://github.com/kuroronrobins"
OPENAI_MODEL = "gpt-5-nano"
OPENAI_MAX_TOKENS = 2048
OPENAI_REASONING_EFFORT = "medium"

RELEASE_MANIFEST_PATH = "release/manifest.json"
RELEASE_LATEST_DIR = "release/latest"

# Do not modify the encoded secret per user instruction.
ENCODED_OPENAI_KEY = (
    "c2stcHJvai13Q09HbnZOTFJ2MVE3VmU0UGFoU3VHU180M0xBUk9FRERTLTlBeExGM0hDRjZYN1NybURRV0sy"
    "bW90LVp3b3ZmY25DQ3Q4VERnSVQzQmxia0ZKSUY1cHd1c1pzUFEyQlFQNzlNaVJpbWxKUlY5c3VmUFBKZDZCdDhLdFpfRzlFSFBhdGQ0SVlRWGZCYkdRdVVJV2pPY2lnSUtXa0E="
)

_openai_client: Optional["OpenAI"] = None


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def config_path() -> Path:
    return base_dir() / APP_DATA_FILE


@dataclass
class AppConfig:
    remote_url: str = ""
    remote_base: str = REMOTE_BASE_DEFAULT
    app_name: str = APP_NAME_DEFAULT
    exe_name: str = "main"
    exclude: List[str] = None  # type: ignore[assignment]
    stage_protected: List[str] = None  # type: ignore[assignment]
    stable_version: str = "0.0.0"
    develop_version: str = "0.0.0"

    def __post_init__(self) -> None:
        # Merge defaults so existing configs auto-upgrade.
        self.exclude = (self.exclude or []).copy()
        self.stage_protected = (self.stage_protected or []).copy()
        for pat in EXCLUDE_DEFAULT:
            if pat not in self.exclude:
                self.exclude.append(pat)
        for pat in STAGE_PROTECTED_DEFAULT:
            if pat not in self.stage_protected:
                self.stage_protected.append(pat)

    @classmethod
    def from_dict(cls, data: Dict) -> "AppConfig":
        version = data.get("version", {}) or {}
        paths = data.get("paths", {}) or {}
        app_info = data.get("app", {}) or {}
        return cls(
            remote_url=str(data.get("remote_url") or version.get("remote_url") or ""),
            remote_base=str(app_info.get("remote_base") or data.get("remote_base") or REMOTE_BASE_DEFAULT),
            app_name=str(app_info.get("name") or data.get("app_name") or APP_NAME_DEFAULT),
            exe_name=str(app_info.get("exe_name") or data.get("exe_name") or "main"),
            exclude=list(paths.get("exclude") or EXCLUDE_DEFAULT),
            stage_protected=list(paths.get("stage_protected") or STAGE_PROTECTED_DEFAULT),
            stable_version=str(version.get("stable") or "0.0.0"),
            develop_version=str(version.get("develop") or "0.0.0"),
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "app": {"name": self.app_name, "exe_name": self.exe_name, "remote_base": self.remote_base},
            "remote_url": self.remote_url,
            "version": {"stable": self.stable_version, "develop": self.develop_version},
            "paths": {"exclude": self.exclude, "stage_protected": self.stage_protected},
        }


def load_config() -> AppConfig:
    path = config_path()
    if not path.exists():
        cfg = AppConfig()
        save_config(cfg)
        return cfg
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cfg = AppConfig.from_dict(data)
    except Exception:
        cfg = AppConfig()
    save_config(cfg)
    return cfg


def save_config(cfg: AppConfig) -> None:
    config_path().write_text(json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------
# OpenAI helpers
# ------------------------------------------------------------
def decode_openai_key() -> Optional[str]:
    if not ENCODED_OPENAI_KEY:
        return None
    try:
        return base64.b64decode(ENCODED_OPENAI_KEY).decode("utf-8")
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
        return None
    if _openai_client is not None:
        return _openai_client
    api_key = get_api_key()
    if not api_key:
        return None
    _openai_client = OpenAI(api_key=api_key)
    return _openai_client


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
            label = name.replace("_", " ")
            parts.append(f"{label} {val}")
    return "OpenAI usage: " + " | ".join(parts) if parts else ""


def call_openai_for_summary(diff_text: str, fallback: str) -> Tuple[str, str]:
    client = get_openai_client()
    if client is None:
        return fallback, ""
    system_prompt = AI_COMMIT_SUMMARY
    user_prompt = textwrap.dedent(
        f"""
        以下のステージ済みdiffを要約してコミットメッセージを作成してください。
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
            reasoning_effort=OPENAI_REASONING_EFFORT,
        )
        text = extract_openai_text(completion)
        usage = format_openai_usage(getattr(completion, "usage", None))
        return text or fallback, usage
    except Exception:
        return fallback, ""


# ------------------------------------------------------------
# CLI helpers
# ------------------------------------------------------------
def input_with_default(message: str, default: str) -> str:
    val = input(f"{message} [{default}]: ").strip()
    return val or default


def confirm(message: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    ans = input(f"{message} {suffix}: ").strip().lower()
    if not ans:
        return default
    return ans in ("y", "yes")


def print_header(cfg: AppConfig) -> None:
    print("======================================")
    print(f"{cfg.app_name} {APP_VERSION}")
    print("======================================")
    print(f"Remote: {cfg.remote_url or '(not set)'}")
    print(f"Stable version:  {cfg.stable_version}")
    print(f"Develop version: {cfg.develop_version}")
    print("--------------------------------------")


# ------------------------------------------------------------
# Git helpers
# ------------------------------------------------------------
def run_git(args: Iterable[str], check: bool = False, capture_output: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git", *args]
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture_output,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def ensure_git_repo() -> None:
    cp = run_git(["rev-parse", "--is-inside-work-tree"])
    if cp.returncode == 0 and cp.stdout.strip() == "true":
        return
    print("[INFO] Not a git repository. Initializing...")
    run_git(["init"], check=True)


def ensure_git_identity() -> None:
    def config_get(key: str) -> str:
        cp = run_git(["config", "--get", key])
        if cp.returncode != 0:
            return ""
        return cp.stdout.strip()

    name = config_get("user.name")
    email = config_get("user.email")
    if name and email:
        return
    print("[INFO] git user.name / user.email are not configured.")
    if not name:
        new_name = input("user.name: ").strip()
        if new_name:
            run_git(["config", "user.name", new_name])
    if not email:
        new_email = input("user.email: ").strip()
        if new_email:
            run_git(["config", "user.email", new_email])


def ensure_gitignore(cfg: AppConfig) -> None:
    path = base_dir() / ".gitignore"
    existing: List[str] = []
    if path.exists():
        existing = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    added = False
    for pat in cfg.exclude:
        if pat not in existing:
            existing.append(pat)
            added = True
    if added:
        path.write_text("\n".join(existing) + "\n", encoding="utf-8")

# ------------------------------------------------------------
# Release helpers
# ------------------------------------------------------------
def normalize_remote_url(raw: str, remote_base: str) -> str:
    """
    Accepts:
      - owner/repo
      - repo
      - https://github.com/owner/repo(.git)
      - git@github.com:owner/repo(.git)
    Returns a URL suitable for `git remote add`, typically https://.../.git or git@...:... .git.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    # Already a git URL
    if s.startswith("git@github.com:"):
        return s if s.endswith(".git") else s + ".git"
    if s.startswith("https://github.com/") or s.startswith("http://github.com/"):
        s = s.replace("http://", "https://", 1)
        return s if s.endswith(".git") else s + ".git"

    # owner/repo or repo
    if "/" in s:
        owner_repo = s
        if owner_repo.endswith(".git"):
            owner_repo = owner_repo[:-4]
        return f"https://github.com/{owner_repo}.git"

    base = (remote_base or "").strip().rstrip("/")
    if not base:
        base = "https://github.com"
    # If remote_base points to a user/org page, keep it.
    return f"{base}/{s}.git"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_release_manifest(cfg: AppConfig, version: str, exe_rel_path: str, exe_file: Path) -> Path:
    manifest_path = base_dir() / RELEASE_MANIFEST_PATH
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "app": {"name": cfg.app_name, "exe_name": cfg.exe_name},
        "channel": "stable",
        "version": version,
        "exe_path": exe_rel_path.replace("\\", "/"),
        "sha256": sha256_file(exe_file),
        "size": exe_file.stat().st_size,
        "published_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    manifest_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest_path


def place_latest_exe(cfg: AppConfig, built_exe: Path) -> Path:
    latest_dir = base_dir() / RELEASE_LATEST_DIR
    latest_dir.mkdir(parents=True, exist_ok=True)
    name = cfg.exe_name if cfg.exe_name.lower().endswith(".exe") else cfg.exe_name + ".exe"
    dst = latest_dir / name
    shutil.copy2(built_exe, dst)
    return dst


def build_exe(cfg: AppConfig, extra_pyinstaller_opts: Optional[List[str]] = None) -> Optional[Path]:
    """Build a onefile exe into dist/ and return the path if successful.

    The executable's *self version* is derived from the embedded config (version.stable)
    inside the bootstrap at runtime.
    """
    ensure_git_repo()
    exe_name = cfg.exe_name or "main"
    if shutil.which("pyinstaller") is None:
        print("[WARN] pyinstaller is not available in PATH. Skipping build.")
        return None
    opts = extra_pyinstaller_opts or []
    build_dir = base_dir() / "build"
    dist_dir = base_dir() / "dist"
    build_dir.mkdir(parents=True, exist_ok=True)
    dist_dir.mkdir(parents=True, exist_ok=True)

    exe_path = dist_dir / (exe_name if exe_name.lower().endswith(".exe") else exe_name + ".exe")
    if exe_path.exists():
        try:
            exe_path.unlink()
        except PermissionError:
            print(f"[ERROR] dist exe is in use. Close running {exe_path.name} and retry the build.")
            return None

    # Clean previous spec in root if any
    spec_root = base_dir() / "main.spec"
    if spec_root.exists():
        spec_root.unlink(missing_ok=True)

    bootstrap_template = (base_dir() / "_bootstrap.py").read_text(encoding="utf-8")
    embedded_config_json = json.dumps(cfg.to_dict(), ensure_ascii=False)
    # Embed config JSON into the bootstrap source. The updater reads version.stable from this config.
    bootstrap_text = bootstrap_template.replace('"__EMBEDDED_CONFIG_JSON__"', json.dumps(embedded_config_json))
    bootstrap_path = build_dir / "_bootstrap_embedded.py"
    bootstrap_path.write_text(bootstrap_text, encoding="utf-8")

    data_sep = ";" if os.name == "nt" else ":"
    cmd = [
        "pyinstaller",
        "--onefile",
        "--clean",
        "--noconfirm",
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(build_dir),
        "--specpath",
        str(build_dir),
        "--name",
        exe_name,
        "--add-data",
        f"{config_path()}{data_sep}.",
        "--add-data",
        f"{base_dir() / 'main.py'}{data_sep}.",
        str(bootstrap_path),
    ]
    cmd.extend(opts)
    print(f"[INFO] Running: {' '.join(cmd)}")
    cp = subprocess.run(cmd, cwd=base_dir())
    if cp.returncode != 0:
        print("[ERROR] Build failed.")
        return None
    if exe_path.exists():
        print(f"[INFO] Build complete: {exe_path}")
        return exe_path
    print("[WARN] Build finished but exe not found in dist/")
    return None



def ensure_remote(cfg: AppConfig) -> str:
    cp = run_git(["remote", "get-url", DEFAULT_REMOTE_NAME])
    if cp.returncode == 0:
        url = cp.stdout.strip()
        if url and url != cfg.remote_url:
            cfg.remote_url = url
            save_config(cfg)
        return url

    print("[INFO] Remote 'origin' is not set.")
    raw = input_with_default("GitHub repo (owner/repo or full URL)", cfg.remote_url or "")
    while not raw.strip():
        raw = input_with_default("GitHub repo (owner/repo or full URL)", cfg.remote_url or "")
    url = normalize_remote_url(raw, cfg.remote_base)
    run_git(["remote", "add", DEFAULT_REMOTE_NAME, url], check=True)
    cfg.remote_url = url
    save_config(cfg)
    return url


def current_branch() -> str:
    cp = run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    if cp.returncode != 0:
        return ""
    return cp.stdout.strip()


def checkout_branch(branch: str) -> bool:
    cp = run_git(["rev-parse", "--verify", branch])
    if cp.returncode == 0:
        res = run_git(["checkout", branch])
        if res.returncode != 0 and res.stderr:
            print(res.stderr.strip())
        return res.returncode == 0
    res = run_git(["checkout", "-b", branch])
    if res.returncode != 0 and res.stderr:
        print(res.stderr.strip())
    return res.returncode == 0


def working_tree_dirty() -> bool:
    cp = run_git(["status", "--porcelain"])
    return any(ln.strip() for ln in cp.stdout.splitlines())


def fetch_branch(branch: str) -> None:
    run_git(["fetch", DEFAULT_REMOTE_NAME, branch])


def has_upstream() -> bool:
    cp = run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    return cp.returncode == 0


def list_changes() -> List[Tuple[str, str]]:
    cp = run_git(["status", "--porcelain"])
    results: List[Tuple[str, str]] = []
    for raw in cp.stdout.splitlines():
        if not raw.strip():
            continue
        status = raw[0:2]
        path = raw[3:] if len(raw) > 3 else raw[2:]
        results.append((status.strip(), path.strip()))
    return results


def is_excluded(path: str, patterns: List[str]) -> bool:
    normalized = path.replace("\\", "/")
    for pat in patterns:
        pat_norm = pat.replace("\\", "/")
        if pat_norm.endswith("/"):
            if normalized == pat_norm.rstrip("/") or normalized.startswith(pat_norm):
                return True
        else:
            if Path(normalized).match(pat_norm):
                return True
    return False


def stage_with_filters(cfg: AppConfig, changes: List[Tuple[str, str]]) -> Tuple[List[str], List[str]]:
    staged: List[str] = []
    skipped: List[str] = []
    for _, path in changes:
        if is_excluded(path, cfg.exclude) or is_excluded(path, cfg.stage_protected):
            skipped.append(path)
            continue
        cp = run_git(["add", "--", path])
        if cp.returncode == 0:
            staged.append(path)
        else:
            skipped.append(path)
    return staged, skipped


def diff_stat() -> str:
    cp = run_git(["diff", "--cached", "--stat"], capture_output=True)
    return cp.stdout.strip()


def make_diff_summary(cfg: AppConfig) -> Tuple[str, str]:
    cp = run_git(["diff", "--cached", "--numstat"])
    lines = [ln for ln in cp.stdout.splitlines() if ln.strip()]
    if not lines:
        return "No changes", ""
    total_add = total_del = 0
    entries: List[str] = []
    skip_patterns = list({p for p in (cfg.exclude + cfg.stage_protected)})
    for ln in lines:
        parts = ln.split("\t")
        if len(parts) < 3:
            continue
        add = int(parts[0]) if parts[0].isdigit() else 0
        delete = int(parts[1]) if parts[1].isdigit() else 0
        path = parts[2]
        if is_excluded(path, skip_patterns):
            continue
        total_add += add
        total_del += delete
        entries.append(f"{path}: +{add}/-{delete}")
    if not entries:
        return "No diff (excluded only)", ""
    summary = f"{len(entries)} files (+{total_add}/-{total_del})"
    return summary, "\n".join(entries)


def list_remote_commits(branch: str, limit: int = 15) -> List[Tuple[str, str, str]]:
    cp = run_git(
        ["log", f"{DEFAULT_REMOTE_NAME}/{branch}", f"-{limit}", "--date=format:%Y-%m-%d %H:%M", "--pretty=format:%h|%ad|%s"]
    )
    commits: List[Tuple[str, str, str]] = []
    for line in cp.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("|", 2)
        if len(parts) == 3:
            commits.append((parts[0], parts[1], parts[2]))
    return commits


def remote_head_timestamp(branch: str) -> int:
    cp = run_git(["show", "-s", "--format=%ct", f"{DEFAULT_REMOTE_NAME}/{branch}"])
    if cp.returncode != 0:
        return 0
    try:
        return int(cp.stdout.strip())
    except ValueError:
        return 0


def resolve_latest_remote_branch() -> str:
    fetch_branch(DEFAULT_STABLE_BRANCH)
    fetch_branch(DEFAULT_DEVELOP_BRANCH)
    ts_stable = remote_head_timestamp(DEFAULT_STABLE_BRANCH)
    ts_dev = remote_head_timestamp(DEFAULT_DEVELOP_BRANCH)
    return DEFAULT_DEVELOP_BRANCH if ts_dev > ts_stable else DEFAULT_STABLE_BRANCH


def push_to_remote(branch: str) -> Tuple[bool, str]:
    args = ["push"]
    if not has_upstream():
        args = ["push", "--set-upstream", DEFAULT_REMOTE_NAME, branch]
    cp = run_git(args)
    if cp.returncode != 0:
        return False, cp.stderr
    return True, ""


def handle_push_failure(branch: str, stderr: str) -> None:
    print("[WARN] Push failed.")
    if "non-fast-forward" in (stderr or "").lower():
        print("Remote has newer commits than local.")
    print("Select how to proceed:")
    print(" [1] Force push local state to remote (overwrite remote)")
    print(" [2] Fetch and rebase onto remote, then push")
    print(" [3] Abort")
    choice = input("Number (3): ").strip() or "3"
    if choice == "1":
        print("[INFO] Force pushing...")
        cp = run_git(["push", "--force", DEFAULT_REMOTE_NAME, branch])
        if cp.returncode != 0:
            print(cp.stderr)
        else:
            print("[INFO] Force push completed.")
    elif choice == "2":
        print("[INFO] Fetching and rebasing...")
        if run_git(["fetch", DEFAULT_REMOTE_NAME, branch]).returncode != 0:
            print("[ERROR] Fetch failed.")
            return
        if run_git(["rebase", f"{DEFAULT_REMOTE_NAME}/{branch}"]).returncode != 0:
            print("[ERROR] Rebase failed; please resolve manually.")
            return
        if run_git(["push"]).returncode != 0:
            print("[ERROR] Push after rebase failed.")
        else:
            print("[INFO] Push after rebase completed.")
    else:
        print("[INFO] Push aborted by user.")


def sync_develop_with_stable() -> None:
    """Mirror stable to develop after a successful stable push."""
    original = current_branch()
    if not checkout_branch(DEFAULT_DEVELOP_BRANCH):
        print("[WARN] Could not switch to develop to mirror stable.")
        return
    if run_git(["reset", "--hard", DEFAULT_STABLE_BRANCH]).returncode != 0:
        print("[WARN] Failed to reset develop to stable.")
        return
    push_cmd = ["push"] if has_upstream() else ["push", "--set-upstream", DEFAULT_REMOTE_NAME, DEFAULT_DEVELOP_BRANCH]
    if run_git(push_cmd).returncode != 0:
        print("[WARN] Failed to push develop after mirroring stable.")
    else:
        print("[INFO] develop mirrored to stable and pushed.")
    if original and original != DEFAULT_DEVELOP_BRANCH:
        checkout_branch(original)


# ------------------------------------------------------------
# Pull / Push / Build flows
# ------------------------------------------------------------
def pull_latest(branch: str) -> None:
    if not checkout_branch(branch):
        print("[ERROR] Failed to check out branch.")
        return
    cp = run_git(["pull", "--ff-only", DEFAULT_REMOTE_NAME, branch])
    if cp.returncode != 0:
        print(cp.stderr)
    else:
        print(f"[INFO] Pulled latest for {branch}.")


def checkout_past_commit(branch: str) -> None:
    commits = list_remote_commits(branch)
    if not commits:
        print(f"[WARN] Could not fetch history for {branch}.")
        return
    print(f"[INFO] Recent commits on {branch}:")
    for idx, (sha, date, msg) in enumerate(commits, 1):
        print(f"[{idx:02}] {date} {sha} {msg}")
    sel = input(f"Select number (1-{len(commits)}) or Enter to cancel: ").strip()
    if not sel.isdigit():
        print("[INFO] Cancelled.")
        return
    num = int(sel)
    if num < 1 or num > len(commits):
        print("[WARN] Out of range.")
        return
    sha, date, msg = commits[num - 1]
    print(f"[INFO] Checking out {sha} ({date}) {msg} in detached mode.")
    if working_tree_dirty() and not confirm("Working tree has changes. Proceed with checkout --detach?", default=False):
        return
    cp = run_git(["checkout", "--detach", sha])
    if cp.returncode != 0:
        print(cp.stderr)
    else:
        print("[INFO] Detached checkout completed.")


def pull_flow(cfg: AppConfig) -> None:
    ensure_git_repo()
    ensure_git_identity()
    remote_url = ensure_remote(cfg)
    if not remote_url:
        print("[ERROR] Remote not configured.")
        return
    target = prompt_pull_branch()
    branch = resolve_latest_remote_branch() if target == "latest" else target
    fetch_branch(branch)
    print("[1] Pull latest")
    print("[2] Select a past commit")
    choice = input("Number (1): ").strip() or "1"
    if choice == "2":
        checkout_past_commit(branch)
    else:
        pull_latest(branch)



def push_flow(cfg: AppConfig) -> None:
    ensure_git_repo()
    ensure_git_identity()
    ensure_gitignore(cfg)

    remote_url = ensure_remote(cfg)
    if not remote_url:
        print("[ERROR] Remote not configured.")
        return
    # Note: even if there are no changes yet, a version bump and/or stable release build may create changes.

    branch = prompt_push_branch()
    if not checkout_branch(branch):
        print("[ERROR] Could not switch to target branch.")
        return

    if branch == DEFAULT_STABLE_BRANCH:
        cfg.stable_version = input_with_default("Stable version", cfg.stable_version)
    else:
        cfg.develop_version = input_with_default("Develop version", cfg.develop_version)
    save_config(cfg)


    did_release = False

    # For stable pushes, optionally produce/update release artifacts so the packaged exe can self-update from a fixed path.
    if branch == DEFAULT_STABLE_BRANCH:
        if confirm("Build & publish latest exe + manifest for auto-update?", default=False):
            print(f"[INFO] Publishing release artifacts with version {cfg.stable_version or '0.0.0'} (build happens now).")
            built = build_exe(cfg)
            if built:
                latest_exe = place_latest_exe(cfg, built)
                exe_rel = f"{RELEASE_LATEST_DIR}/{latest_exe.name}"
                manifest = write_release_manifest(cfg, cfg.stable_version or "0.0.0", exe_rel, latest_exe)
                # Force stage release artifacts (even if stage-protected).
                run_git(["add", "--", str(manifest.relative_to(base_dir()))])
                run_git(["add", "--", str(latest_exe.relative_to(base_dir()))])
                did_release = True
                print(f"[INFO] Release updated: {manifest} / {latest_exe}")
            else:
                print("[WARN] Skipped publishing release artifacts (pyinstaller unavailable or build failed).")
        else:
            print("[INFO] Skipped building/publishing release artifacts for this stable push.")
            print("[INFO] Auto-update checks release/manifest.json; publish release artifacts when you want clients to update.")

    # Refresh changes after optional release build
    changes = list_changes()
    staged, skipped = stage_with_filters(cfg, changes)

    if skipped:
        print("[INFO] Skipped (excluded/protected):")
        for path in skipped:
            print(f"  - {path}")
    if not staged and branch != DEFAULT_STABLE_BRANCH:
        print("[INFO] Nothing staged after filtering.")
        return

    # Print staged list
    print("[INFO] Staged files:")
    for path in staged:
        print(f"  - {path}")
    # Also show staged release artifacts if any
    if branch == DEFAULT_STABLE_BRANCH:
        cp = run_git(["diff", "--cached", "--name-only"])
        if cp.returncode == 0:
            for ln in [x.strip() for x in cp.stdout.splitlines() if x.strip()]:
                if ln.startswith("release/"):
                    print(f"  - {ln}")

    stat = diff_stat()
    if stat:
        print("[INFO] Diff stat:")
        print(stat)

    summary, diff_text = make_diff_summary(cfg)
    commit_body, usage = call_openai_for_summary(diff_text, summary)
    commit_msg = f"{AUTO_COMMIT_PREFIX} {datetime.datetime.now():%Y-%m-%d %H:%M} - {commit_body}"
    if usage:
        print(usage)
    print(f"Commit message\n{commit_msg}")
    if not confirm("Commit with this AI-generated message?", default=False):
        return

    cp = run_git(["commit", "-m", commit_msg])
    if cp.returncode != 0:
        print(cp.stderr)
        return

    ok, err = push_to_remote(branch)
    if ok:
        print("[INFO] Push completed.")
        if branch == DEFAULT_STABLE_BRANCH:
            sync_develop_with_stable()
    else:
        handle_push_failure(branch, err)



def build_flow(cfg: AppConfig) -> None:
    """
    Manual build (dev/testing).
    - This menu is for local testing only.
    - For publishing to GitHub (auto-update), use: Push -> Stable, then choose to publish release artifacts.
      (That flow decides the version first, then builds, so the published exe/manifest versions always match.)
    - The updater compares versions using the embedded config field: version.stable.
      If you build locally without setting a version, it will be treated as 0.0.0 and you'll always see an update prompt.
    """
    print("[INFO] Build exe (local test).")
    print("[INFO] To publish a new version for auto-update, use: Push -> Stable and answer YES to publish.")
    print("[INFO] That flow builds AFTER the version is decided, preventing version mismatches.")

    # Let users embed a version for local builds to avoid showing 0.0.0 all the time.
    embed_default = cfg.stable_version or "0.0.0"
    embed = input_with_default("Version for this build (embedded as version.stable)", embed_default).strip() or embed_default
    cfg.stable_version = embed
    if cfg.stable_version == "0.0.0":
        print("[WARN] Version is 0.0.0. If you want the build to identify itself, set a stable version and rebuild.")

    additional = input("Additional PyInstaller options (empty for none): ").strip()
    opts = additional.split() if additional else []
    _ = build_exe(cfg, extra_pyinstaller_opts=opts)


# ------------------------------------------------------------
# Configure / Status / Menus
# ------------------------------------------------------------

def configure_flow(cfg: AppConfig) -> AppConfig:
    print("[CONFIG] Basic settings (minimal).")
    cfg.app_name = input_with_default("App name", cfg.app_name)
    cfg.exe_name = input_with_default("Exe base name", cfg.exe_name)
    raw_remote = input_with_default("Remote (owner/repo or URL)", cfg.remote_url)
    if raw_remote.strip():
        cfg.remote_url = normalize_remote_url(raw_remote, cfg.remote_base)
    save_config(cfg)
    ensure_gitignore(cfg)

    if confirm("Edit advanced path filters (exclude / stage-protected)?", default=False):
        exclude = input_with_default("Exclude paths (comma separated)", ",".join(cfg.exclude))
        stage_protected = input_with_default(
            "Stage-protected patterns (comma separated)", ",".join(cfg.stage_protected)
        )
        cfg.exclude = [p.strip() for p in exclude.split(",") if p.strip()] or EXCLUDE_DEFAULT.copy()
        cfg.stage_protected = (
            [p.strip() for p in stage_protected.split(",") if p.strip()] or STAGE_PROTECTED_DEFAULT.copy()
        )
        save_config(cfg)
        ensure_gitignore(cfg)

    print("[INFO] Configuration saved.")
    return cfg


def status_flow(cfg: AppConfig) -> None:
    ensure_git_repo()
    ensure_git_identity()
    ensure_gitignore(cfg)
    print_header(cfg)
    branch = current_branch()
    print(f"Current branch: {branch or '(detached)'}")
    changes = list_changes()
    if not changes:
        print("Working tree: clean")
    else:
        print("Working tree changes:")
        for status, path in changes:
            print(f"  {status:>2} {path}")


def prompt_pull_branch() -> str:
    print("Pull target:")
    print(" [1] Stable")
    print(" [2] Develop")
    print(" [3] Latest (newer of stable/develop)")
    choice = input("Number (1): ").strip()
    if choice == "2":
        return DEFAULT_DEVELOP_BRANCH
    if choice == "3":
        return "latest"
    return DEFAULT_STABLE_BRANCH


def prompt_push_branch() -> str:
    print("Push target:")
    print(" [1] Stable")
    print(" [2] Develop")
    choice = input("Number (1): ").strip()
    if choice == "2":
        return DEFAULT_DEVELOP_BRANCH
    return DEFAULT_STABLE_BRANCH


# ------------------------------------------------------------
# Main menu / args
# ------------------------------------------------------------
def main_menu(cfg: AppConfig) -> None:
    while True:
        print_header(cfg)
        print("[1] Status")
        print("[2] Pull")
        print("[3] Push")
        print("[4] Build exe (local test)")
        print("[5] Configure")
        print("[6] Exit")
        choice = input("Number: ").strip()
        if choice == "1":
            status_flow(cfg)
        elif choice == "2":
            pull_flow(cfg)
        elif choice == "3":
            push_flow(cfg)
        elif choice == "4":
            build_flow(cfg)
        elif choice == "5":
            cfg = configure_flow(cfg)
        elif choice == "6":
            break
        else:
            print("Please enter 1-6.")
        input("Press Enter to return to menu...")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GitHubSync utility", add_help=True)
    return parser.parse_args()


def main() -> None:
    _ = parse_args()
    cfg = load_config()
    ensure_gitignore(cfg)
    main_menu(cfg)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INFO] Interrupted by user.")
