#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitUp Manager

Project-local Git helper for users who do not want to operate Git directly.
The script/exe lives in the project root, but all Git operations are executed
from a shadow copy under the user's local runtime directory.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as _dt
import difflib
import fnmatch
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - optional dependency
    OpenAI = None  # type: ignore[assignment]


APP_VERSION = "4.0.0"
APP_DATA_FILE = "_app_data.json"
STATE_FILE = ".gitup/state.json"
DEFAULT_REMOTE = "origin"
DEFAULT_BRANCH = "stable"
OPENAI_CREDENTIAL_TARGET = "GitUpManager.OpenAI.APIKey.v1"
OPENAI_CREDENTIAL_USER = "openai"
MAX_AI_DIFF_CHARS = 12000

SYSTEM_FILES = [
    "_gitup.py",
    "GitUp Manager.exe",
    "_app_data.json",
    ".gitignore",
]

OLD_SYSTEM_FILES = [
    "_bootstrap.py",
    "_bootstrap_patch_helper.txt",
    "release/manifest.json",
    "release/latest/main.exe",
]

DEFAULT_EXCLUDES = [
    ".git/",
    ".gitup/",
    "build/",
    "dist/",
    "__pycache__/",
    ".pytest_cache/",
    ".venv/",
    "*.pyc",
    "*.pyo",
    "*.pyd",
    "*.spec",
]

DEFAULT_STAGE_PROTECTED = [
    "_gitup.py",
    "GitUp Manager.exe",
    "_app_data.json",
    "_bootstrap.py",
    "_bootstrap_patch_helper.txt",
    "release/",
    "build/",
    "dist/",
    "__pycache__/",
    ".pytest_cache/",
    "*.pyc",
    "*.pyo",
    "*.pyd",
    "*.spec",
]

DEFAULT_AI_MODELS = {
    "quick_classification": "gpt-5.4-nano",
    "commit_summary": "gpt-5.4-mini",
    "conflict_explanation": "gpt-5.4-mini",
    "deep_diagnosis": "gpt-5.4",
    "codex_grade_review": "gpt-5.5",
}

DEFAULT_REASONING = {
    "quick_classification": "low",
    "commit_summary": "low",
    "conflict_explanation": "medium",
    "deep_diagnosis": "medium",
}

AI_PRESETS = {
    "economy": {
        "quick_classification": "gpt-5.4-nano",
        "commit_summary": "gpt-5.4-nano",
        "conflict_explanation": "gpt-5.4-mini",
        "deep_diagnosis": "gpt-5.4-mini",
        "codex_grade_review": "gpt-5.4",
    },
    "balanced": DEFAULT_AI_MODELS,
    "quality": {
        "quick_classification": "gpt-5.4-mini",
        "commit_summary": "gpt-5.4-mini",
        "conflict_explanation": "gpt-5.4",
        "deep_diagnosis": "gpt-5.4",
        "codex_grade_review": "gpt-5.5",
    },
}


# ---------------------------------------------------------------------------
# Basic path/runtime helpers
# ---------------------------------------------------------------------------


def now_stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def executable_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return Path(__file__).resolve()


def is_windows() -> bool:
    return os.name == "nt"


def path_is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def project_hash(project_root: Path) -> str:
    key = str(project_root.resolve()).lower().encode("utf-8", errors="replace")
    return hashlib.sha256(key).hexdigest()[:16]


def local_runtime_base() -> Path:
    candidates: List[Path] = []
    if is_windows():
        root = os.environ.get("LOCALAPPDATA")
        if root:
            candidates.append(Path(root) / "GitUpManager" / "runtime")
    candidates.append(Path(tempfile.gettempdir()) / "GitUpManager" / "runtime")
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError:
            continue
    return candidates[-1]


def runtime_dir(project_root: Path) -> Path:
    return local_runtime_base() / project_hash(project_root)


def gitup_dir(project_root: Path) -> Path:
    return project_root / ".gitup"


def backups_dir(project_root: Path) -> Path:
    return gitup_dir(project_root) / "backups"


def logs_dir(project_root: Path) -> Path:
    return gitup_dir(project_root) / "logs"


def ensure_runtime_dirs(project_root: Path) -> None:
    for path in (gitup_dir(project_root), backups_dir(project_root), logs_dir(project_root), gitup_dir(project_root) / "runtime"):
        path.mkdir(parents=True, exist_ok=True)


def config_path(project_root: Path) -> Path:
    return project_root / APP_DATA_FILE


def state_path(project_root: Path) -> Path:
    return project_root / STATE_FILE


def rel_path(project_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def decode_path(raw: bytes) -> str:
    return raw.decode("utf-8", errors="surrogateescape")


def normalize_rel(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def print_error(message: str, detail: str = "") -> None:
    print(f"[ERROR] {message}")
    if detail:
        print(detail.strip())


def print_warn(message: str) -> None:
    print(f"[WARN] {message}")


def print_info(message: str) -> None:
    print(f"[INFO] {message}")


def input_default(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def confirm(prompt: str, default: bool = False, non_interactive: bool = False) -> bool:
    if non_interactive:
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    value = input(f"{prompt} {suffix}: ").strip().lower()
    if not value:
        return default
    return value in {"y", "yes", "はい", "1"}


def safe_remove(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def safe_move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        suffix = f".moved_{int(time.time())}"
        dst = dst.with_name(dst.name + suffix)
    shutil.move(str(src), str(dst))


def mask_secrets(text: str) -> str:
    if not text:
        return text
    patterns = [
        r"sk-[A-Za-z0-9_\-]{16,}",
        r"gh[pousr]_[A-Za-z0-9_]{20,}",
        r"github_pat_[A-Za-z0-9_]{20,}",
        r"(?i)(api[_-]?key|token|secret|password)(\s*[:=]\s*)([^\s\"']{8,})",
    ]
    masked = text
    for pat in patterns:
        if pat.startswith("(?i)"):
            masked = re.sub(pat, lambda m: f"{m.group(1)}{m.group(2)}[MASKED]", masked)
        else:
            masked = re.sub(pat, "[MASKED]", masked)
    return masked


def log_event(project_root: Path, message: str) -> None:
    try:
        ensure_runtime_dirs(project_root)
        log_file = logs_dir(project_root) / f"gitup_{_dt.datetime.now():%Y%m%d}.log"
        line = f"{_dt.datetime.now():%Y-%m-%d %H:%M:%S} {mask_secrets(message)}\n"
        with log_file.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Shadow-run
# ---------------------------------------------------------------------------


def remove_shadow_args(argv: Sequence[str]) -> List[str]:
    cleaned: List[str] = []
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg == "--shadow":
            continue
        if arg == "--project-root":
            skip_next = True
            continue
        if arg.startswith("--project-root="):
            continue
        cleaned.append(arg)
    return cleaned


def launch_shadow_or_exit(args: argparse.Namespace, original_argv: Sequence[str]) -> None:
    if args.shadow or os.environ.get("GITUP_SHADOW") == "1":
        return

    source = executable_path()
    project_root = Path(args.project_root).resolve() if args.project_root else source.parent.resolve()
    shadow_root = runtime_dir(project_root)

    try:
        shadow_root.mkdir(parents=True, exist_ok=True)
        if getattr(sys, "frozen", False):
            shadow_target = shadow_root / source.name
            shutil.copy2(source, shadow_target)
            cmd = [str(shadow_target)]
        else:
            shadow_target = shadow_root / "_gitup.py"
            shutil.copy2(source, shadow_target)
            cmd = [sys.executable, str(shadow_target)]

        cmd.extend(["--shadow", "--project-root", str(project_root)])
        cmd.extend(remove_shadow_args(original_argv))

        env = os.environ.copy()
        env["GITUP_SHADOW"] = "1"
        env["GITUP_PROJECT_ROOT"] = str(project_root)
        subprocess.Popen(cmd, cwd=str(shadow_root), env=env)
        print_info("Git操作の安全のため、一時フォルダのshadow実行へ切り替えました。")
        raise SystemExit(0)
    except SystemExit:
        raise
    except Exception as exc:
        print_error("shadow-runに失敗したため、Git操作は実行しません。", str(exc))
        raise SystemExit(10)


# ---------------------------------------------------------------------------
# Configuration/state
# ---------------------------------------------------------------------------


@dataclass
class AppConfig:
    app_name: str = "GitUp Manager"
    exe_name: str = "GitUp Manager.exe"
    remote_url: str = ""
    stable_branch: str = DEFAULT_BRANCH
    develop_branch: str = "develop"
    default_pull: str = DEFAULT_BRANCH
    default_push: str = DEFAULT_BRANCH
    exclude: List[str] = field(default_factory=lambda: DEFAULT_EXCLUDES.copy())
    stage_protected: List[str] = field(default_factory=lambda: DEFAULT_STAGE_PROTECTED.copy())
    ai_enabled: bool = True
    ai_provider: str = "openai"
    ai_api: str = "responses"
    ai_mode: str = "auto"
    ai_models: Dict[str, str] = field(default_factory=lambda: DEFAULT_AI_MODELS.copy())
    ai_reasoning_effort: Dict[str, str] = field(default_factory=lambda: DEFAULT_REASONING.copy())

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "AppConfig":
        app = data.get("app") if isinstance(data.get("app"), dict) else {}
        branches = data.get("branches") if isinstance(data.get("branches"), dict) else {}
        paths = data.get("paths") if isinstance(data.get("paths"), dict) else {}
        ai = data.get("ai") if isinstance(data.get("ai"), dict) else {}

        version = data.get("version") if isinstance(data.get("version"), dict) else {}
        old_app_name = str(data.get("app_name") or "GitUp Manager")
        old_exe = str(data.get("exe_name") or "GitUp Manager.exe")

        stable = str(branches.get("stable") or DEFAULT_BRANCH)
        develop = str(branches.get("develop") or "develop")

        cfg = cls(
            app_name=str(app.get("name") or old_app_name or "GitUp Manager"),
            exe_name=str(app.get("exe_name") or old_exe or "GitUp Manager.exe"),
            remote_url=str(data.get("remote_url") or version.get("remote_url") or ""),
            stable_branch=stable,
            develop_branch=develop,
            default_pull=str(branches.get("default_pull") or stable),
            default_push=str(branches.get("default_push") or stable),
            exclude=list(paths.get("exclude") or DEFAULT_EXCLUDES),
            stage_protected=list(paths.get("stage_protected") or DEFAULT_STAGE_PROTECTED),
            ai_enabled=bool(ai.get("enabled", True)),
            ai_provider=str(ai.get("provider") or "openai"),
            ai_api=str(ai.get("api") or "responses"),
            ai_mode=str(ai.get("mode") or "auto"),
            ai_models=dict(ai.get("models") or DEFAULT_AI_MODELS),
            ai_reasoning_effort=dict(ai.get("reasoning_effort") or DEFAULT_REASONING),
        )
        cfg.merge_defaults()
        return cfg

    def merge_defaults(self) -> None:
        self.exe_name = self.exe_name or "GitUp Manager.exe"
        if not self.exe_name.lower().endswith(".exe"):
            self.exe_name += ".exe"
        for item in DEFAULT_EXCLUDES:
            if item not in self.exclude:
                self.exclude.append(item)
        for item in DEFAULT_STAGE_PROTECTED:
            if item not in self.stage_protected:
                self.stage_protected.append(item)
        for key, value in DEFAULT_AI_MODELS.items():
            self.ai_models.setdefault(key, value)
        for key, value in DEFAULT_REASONING.items():
            self.ai_reasoning_effort.setdefault(key, value)

    def to_dict(self) -> Dict[str, object]:
        return {
            "app": {
                "name": self.app_name,
                "exe_name": self.exe_name,
            },
            "remote_url": self.remote_url,
            "branches": {
                "stable": self.stable_branch,
                "develop": self.develop_branch,
                "default_pull": self.default_pull,
                "default_push": self.default_push,
            },
            "paths": {
                "exclude": self.exclude,
                "stage_protected": self.stage_protected,
            },
            "ai": {
                "enabled": self.ai_enabled,
                "provider": self.ai_provider,
                "api": self.ai_api,
                "mode": self.ai_mode,
                "models": self.ai_models,
                "reasoning_effort": self.ai_reasoning_effort,
            },
        }


def load_config(project_root: Path) -> AppConfig:
    path = config_path(project_root)
    if not path.exists():
        cfg = AppConfig()
        save_config(project_root, cfg)
        return cfg
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cfg = AppConfig.from_dict(data)
    except Exception:
        cfg = AppConfig()
    save_config(project_root, cfg)
    return cfg


def save_config(project_root: Path, cfg: AppConfig) -> None:
    path = config_path(project_root)
    path.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_state(project_root: Path) -> Dict[str, object]:
    path = state_path(project_root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(project_root: Path, state: Dict[str, object]) -> None:
    path = state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ensure_gitignore(project_root: Path, cfg: AppConfig) -> None:
    path = project_root / ".gitignore"
    existing: List[str] = []
    if path.exists():
        existing = [line.rstrip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines()]
    wanted = [
        ".gitup/",
        "build/",
        "dist/",
        "__pycache__/",
        ".pytest_cache/",
        "*.pyc",
        "*.pyo",
        "*.pyd",
        "*.spec",
        ".venv/",
    ]
    changed = False
    for item in wanted:
        if item not in existing:
            existing.append(item)
            changed = True
    if changed or not path.exists():
        path.write_text("\n".join(x for x in existing if x.strip()) + "\n", encoding="utf-8")
    for item in wanted:
        if item not in cfg.exclude:
            cfg.exclude.append(item)
    save_config(project_root, cfg)


# ---------------------------------------------------------------------------
# Git command/status helpers
# ---------------------------------------------------------------------------


@dataclass
class GitChange:
    xy: str
    path: str
    old_path: str = ""

    @property
    def is_untracked(self) -> bool:
        return self.xy == "??"

    @property
    def is_staged(self) -> bool:
        return self.xy[0] not in {" ", "?"}

    @property
    def is_dirty(self) -> bool:
        return self.xy != "!!"

    @property
    def is_conflict(self) -> bool:
        return "U" in self.xy or self.xy in {"AA", "DD", "AU", "UA", "DU", "UD"}


@dataclass
class GitDoctor:
    is_repo: bool = False
    git_dir: str = ""
    remote_url: str = ""
    branch: str = ""
    head: str = ""
    upstream: str = ""
    ahead: int = 0
    behind: int = 0
    diverged: bool = False
    dirty: bool = False
    staged: bool = False
    untracked: int = 0
    changes: List[GitChange] = field(default_factory=list)
    in_merge: bool = False
    in_rebase: bool = False
    in_cherry_pick: bool = False
    conflicts: List[str] = field(default_factory=list)
    system_changes: List[str] = field(default_factory=list)
    shadow_active: bool = False
    exe_lock_possible: bool = False
    recommendation: str = ""
    safety: str = "safe"


def git_env() -> Dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "1")
    env.setdefault("LANG", "C.UTF-8")
    return env


def run_git(
    project_root: Path,
    args: Sequence[str],
    check: bool = False,
    capture_output: bool = True,
    input_text: Optional[str] = None,
    extra_env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    env = git_env()
    if extra_env:
        env.update(extra_env)
    cp = subprocess.run(
        ["git", *args],
        cwd=str(project_root),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=capture_output,
        input=input_text,
        env=env,
    )
    if check and cp.returncode != 0:
        raise subprocess.CalledProcessError(cp.returncode, cp.args, cp.stdout, cp.stderr)
    return cp


def run_git_bytes(project_root: Path, args: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(project_root),
        capture_output=True,
        text=False,
        env=git_env(),
    )


def git_path(project_root: Path, logical_name: str) -> Path:
    cp = run_git(project_root, ["rev-parse", "--git-path", logical_name])
    if cp.returncode != 0:
        return project_root / ".git" / logical_name
    return (project_root / cp.stdout.strip()).resolve()


def inside_git_repo(project_root: Path) -> bool:
    cp = run_git(project_root, ["rev-parse", "--is-inside-work-tree"])
    return cp.returncode == 0 and cp.stdout.strip() == "true"


def parse_porcelain_z(data: bytes) -> List[GitChange]:
    tokens = data.split(b"\0")
    changes: List[GitChange] = []
    i = 0
    while i < len(tokens):
        raw = tokens[i]
        i += 1
        if not raw:
            continue
        if len(raw) < 3:
            continue
        xy = decode_path(raw[:2])
        path = decode_path(raw[3:])
        old_path = ""
        if ("R" in xy or "C" in xy) and i < len(tokens) and tokens[i]:
            old_path = decode_path(tokens[i])
            i += 1
        changes.append(GitChange(xy=xy, path=path, old_path=old_path))
    return changes


def get_changes(project_root: Path) -> List[GitChange]:
    cp = run_git_bytes(project_root, ["status", "--porcelain=v1", "-z", "-uall"])
    if cp.returncode != 0:
        return []
    return parse_porcelain_z(cp.stdout)


def conflict_paths(project_root: Path) -> List[str]:
    cp = run_git_bytes(project_root, ["diff", "--name-only", "--diff-filter=U", "-z"])
    if cp.returncode != 0:
        return []
    return [decode_path(x) for x in cp.stdout.split(b"\0") if x]


def get_branch(project_root: Path) -> str:
    cp = run_git(project_root, ["branch", "--show-current"])
    return cp.stdout.strip() if cp.returncode == 0 else ""


def get_head(project_root: Path, ref: str = "HEAD") -> str:
    cp = run_git(project_root, ["rev-parse", "--verify", ref])
    return cp.stdout.strip() if cp.returncode == 0 else ""


def get_upstream(project_root: Path) -> str:
    cp = run_git(project_root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    return cp.stdout.strip() if cp.returncode == 0 else ""


def remote_branch_ref(branch: str) -> str:
    return f"{DEFAULT_REMOTE}/{branch}"


def ahead_behind(project_root: Path, upstream: str) -> Tuple[int, int]:
    if not upstream:
        return 0, 0
    cp = run_git(project_root, ["rev-list", "--left-right", "--count", f"HEAD...{upstream}"])
    if cp.returncode != 0:
        return 0, 0
    parts = cp.stdout.strip().split()
    if len(parts) != 2:
        return 0, 0
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0


def is_system_path(path: str) -> bool:
    p = normalize_rel(path)
    if p in {normalize_rel(x) for x in SYSTEM_FILES + OLD_SYSTEM_FILES}:
        return True
    return p.startswith("release/") or p.startswith("build/") or p.startswith("dist/")


def diagnose(project_root: Path, cfg: Optional[AppConfig] = None, fetch_known: bool = False) -> GitDoctor:
    doctor = GitDoctor(shadow_active=(os.environ.get("GITUP_SHADOW") == "1"))
    doctor.is_repo = inside_git_repo(project_root)
    doctor.exe_lock_possible = not doctor.shadow_active and (project_root / "GitUp Manager.exe").exists() and is_windows()
    if not doctor.is_repo:
        doctor.recommendation = "初回取得を実行してGitHubからプロジェクトを取得します。"
        doctor.safety = "warning"
        return doctor

    cp = run_git(project_root, ["rev-parse", "--git-dir"])
    doctor.git_dir = cp.stdout.strip() if cp.returncode == 0 else ""
    cp = run_git(project_root, ["remote", "get-url", DEFAULT_REMOTE])
    doctor.remote_url = cp.stdout.strip() if cp.returncode == 0 else ""
    doctor.branch = get_branch(project_root)
    doctor.head = get_head(project_root)
    doctor.upstream = get_upstream(project_root)
    if not doctor.upstream and doctor.branch:
        ref = remote_branch_ref(doctor.branch)
        if get_head(project_root, ref):
            doctor.upstream = ref

    doctor.in_merge = git_path(project_root, "MERGE_HEAD").exists()
    doctor.in_rebase = git_path(project_root, "rebase-merge").exists() or git_path(project_root, "rebase-apply").exists()
    doctor.in_cherry_pick = git_path(project_root, "CHERRY_PICK_HEAD").exists()

    doctor.changes = get_changes(project_root)
    doctor.conflicts = conflict_paths(project_root)
    doctor.dirty = any(ch.is_dirty for ch in doctor.changes)
    doctor.staged = any(ch.is_staged for ch in doctor.changes)
    doctor.untracked = sum(1 for ch in doctor.changes if ch.is_untracked)
    doctor.system_changes = sorted({ch.path for ch in doctor.changes if is_system_path(ch.path)})

    if doctor.upstream:
        doctor.ahead, doctor.behind = ahead_behind(project_root, doctor.upstream)
    doctor.diverged = doctor.ahead > 0 and doctor.behind > 0

    if doctor.conflicts or doctor.in_merge or doctor.in_rebase or doctor.in_cherry_pick:
        doctor.safety = "danger"
        doctor.recommendation = "衝突を解消してください。"
    elif not doctor.remote_url:
        doctor.safety = "warning"
        doctor.recommendation = "remote originを設定してください。"
    elif doctor.diverged:
        doctor.safety = "warning"
        doctor.recommendation = "GitHub版を取り込んでからpushします。"
    elif doctor.behind > 0:
        doctor.safety = "warning"
        doctor.recommendation = "PullでGitHub版を取り込みます。"
    elif doctor.dirty or doctor.ahead > 0:
        doctor.safety = "safe"
        doctor.recommendation = "PushでこのPCの変更をGitHubへ送れます。"
    else:
        doctor.safety = "safe"
        doctor.recommendation = "最新状態です。必要ならPullで確認します。"
    return doctor


def print_doctor(project_root: Path, cfg: AppConfig, doctor: GitDoctor) -> None:
    print("現在の状態:")
    print(f"  プロジェクト: {project_root.name}")
    print(f"  ブランチ: {doctor.branch or '(未取得/未選択)'}")
    if not doctor.is_repo:
        print("  GitHub状態: 未取得")
    elif doctor.diverged:
        print(f"  GitHub状態: 分岐あり (このPC +{doctor.ahead} / GitHub +{doctor.behind})")
    else:
        print(f"  GitHub状態: このPC +{doctor.ahead} / GitHub +{doctor.behind}")
    print(f"  このPCの変更: {len([c for c in doctor.changes if c.is_dirty])}")
    print(f"  安全度: {doctor.safety}")
    print("")
    print("おすすめ:")
    print(f"  {doctor.recommendation}")


def ensure_remote(project_root: Path, cfg: AppConfig, non_interactive: bool = False) -> bool:
    cp = run_git(project_root, ["remote", "get-url", DEFAULT_REMOTE])
    if cp.returncode == 0 and cp.stdout.strip():
        url = cp.stdout.strip()
        if cfg.remote_url != url:
            cfg.remote_url = url
            save_config(project_root, cfg)
        return True

    if not cfg.remote_url and non_interactive:
        return False
    if not cfg.remote_url:
        cfg.remote_url = input_default("GitHub URL (例: https://github.com/owner/repo.git)", "")
        save_config(project_root, cfg)
    if not cfg.remote_url:
        return False
    cp = run_git(project_root, ["remote", "add", DEFAULT_REMOTE, cfg.remote_url])
    if cp.returncode != 0:
        print_error("remote originを設定できませんでした。", cp.stderr)
        return False
    return True


def ensure_identity(project_root: Path, non_interactive: bool = False) -> None:
    name = run_git(project_root, ["config", "--get", "user.name"]).stdout.strip()
    email = run_git(project_root, ["config", "--get", "user.email"]).stdout.strip()
    if name and email:
        return
    print_warn("Gitの user.name / user.email が未設定です。")
    if non_interactive:
        return
    if not name:
        value = input_default("user.name", getpass.getuser())
        if value:
            run_git(project_root, ["config", "user.name", value])
    if not email:
        value = input_default("user.email", "")
        if value:
            run_git(project_root, ["config", "user.email", value])


def path_matches(path: str, patterns: Iterable[str]) -> bool:
    p = normalize_rel(path)
    for pat in patterns:
        q = pat.replace("\\", "/").strip()
        if not q:
            continue
        if q.endswith("/"):
            base = q.strip("/")
            if p == base or p.startswith(base + "/"):
                return True
        if fnmatch.fnmatch(p, q):
            return True
    return False


# ---------------------------------------------------------------------------
# Backup/recovery
# ---------------------------------------------------------------------------


def copy_into_backup(project_root: Path, rel: str, dest_root: Path) -> None:
    rel_norm = normalize_rel(rel)
    if not rel_norm or rel_norm.startswith(".git/") or rel_norm.startswith(".gitup/backups/"):
        return
    src = (project_root / rel_norm).resolve()
    if not path_is_relative_to(src, project_root) or not src.exists():
        return
    dst = dest_root / rel_norm
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir() and not src.is_symlink():
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git", ".gitup"))
    else:
        shutil.copy2(src, dst)


def create_backup(project_root: Path, operation: str, extra_paths: Optional[Iterable[str]] = None) -> Path:
    ensure_runtime_dirs(project_root)
    backup = backups_dir(project_root) / f"{now_stamp()}_{operation}"
    files_dir = backup / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    doctor = diagnose(project_root)
    paths = {ch.path for ch in doctor.changes if ch.path}
    paths.update(ch.old_path for ch in doctor.changes if ch.old_path)
    paths.update(SYSTEM_FILES)
    if extra_paths:
        paths.update(extra_paths)

    for item in sorted(paths):
        copy_into_backup(project_root, item, files_dir)

    status = run_git(project_root, ["status", "--short", "--branch"]) if doctor.is_repo else None
    metadata = {
        "operation": operation,
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "project_root": str(project_root),
        "branch": doctor.branch,
        "head": doctor.head,
        "upstream": doctor.upstream,
        "ahead": doctor.ahead,
        "behind": doctor.behind,
        "remote_head": get_head(project_root, doctor.upstream) if doctor.upstream else "",
    }
    (backup / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if status is not None:
        (backup / "git_status.txt").write_text(mask_secrets(status.stdout + status.stderr), encoding="utf-8")
    print_info(f"バックアップを作成しました: {backup}")
    log_event(project_root, f"backup created: {backup.name} operation={operation}")
    return backup


def list_backups(project_root: Path) -> List[Path]:
    root = backups_dir(project_root)
    if not root.exists():
        return []
    return sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name, reverse=True)


def restore_backup(project_root: Path, backup: Path) -> None:
    files = backup / "files"
    if not files.exists():
        print_error("バックアップ内にfilesがありません。")
        return
    for src in files.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(files)
        dst = project_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    print_info("バックアップからファイルを復元しました。削除済みファイルの完全な巻き戻しが必要な場合は診断を確認してください。")


def recovery_menu(project_root: Path) -> None:
    while True:
        print("復旧メニュー")
        print("[1] 直前の操作を元に戻す")
        print("[2] バックアップ一覧を見る")
        print("[3] 指定バックアップから復元")
        print("[4] ログを見る")
        print("[5] 戻る")
        choice = input("番号: ").strip()
        backups = list_backups(project_root)
        if choice == "1":
            if not backups:
                print_warn("バックアップがありません。")
            else:
                restore_backup(project_root, backups[0])
        elif choice == "2":
            if not backups:
                print_warn("バックアップがありません。")
            for i, path in enumerate(backups, 1):
                print(f"[{i}] {path.name}")
        elif choice == "3":
            for i, path in enumerate(backups, 1):
                print(f"[{i}] {path.name}")
            sel = input("番号: ").strip()
            if sel.isdigit() and 1 <= int(sel) <= len(backups):
                restore_backup(project_root, backups[int(sel) - 1])
        elif choice == "4":
            log_files = sorted(logs_dir(project_root).glob("*.log"))[-3:]
            for log in log_files:
                print(f"--- {log.name} ---")
                print(log.read_text(encoding="utf-8", errors="replace")[-4000:])
        elif choice == "5":
            return


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def branch_default(cfg: AppConfig, pull: bool = True) -> str:
    value = cfg.default_pull if pull else cfg.default_push
    return value or cfg.stable_branch or DEFAULT_BRANCH


def bootstrap_flow(project_root: Path, cfg: AppConfig, no_restart: bool = False, non_interactive: bool = False) -> bool:
    if inside_git_repo(project_root):
        print_info("すでにGitリポジトリです。bootstrapは不要です。")
        return True
    if not cfg.remote_url:
        if non_interactive:
            print_error("remote_urlが_app_data.jsonにありません。")
            return False
        cfg.remote_url = input_default("GitHub URL", "")
        save_config(project_root, cfg)
    if not cfg.remote_url:
        print_error("remote_urlが未設定です。")
        return False

    branch = branch_default(cfg, pull=True)
    if not non_interactive:
        branch = input_default("取得するブランチ", branch)

    ensure_runtime_dirs(project_root)
    backup = backups_dir(project_root) / f"{now_stamp()}_bootstrap_seed"
    seed_dir = backup / "seed"
    failed_dir = backup / "failed_partial"
    seed_dir.mkdir(parents=True, exist_ok=True)
    (backup / "metadata.json").write_text(
        json.dumps(
            {
                "operation": "bootstrap",
                "remote_url": cfg.remote_url,
                "branch": branch,
                "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    moved_seed = False
    temp_parent = Path(tempfile.mkdtemp(prefix="gitup_bootstrap_"))
    clone_dir = temp_parent / "repo"
    try:
        current_items = [p for p in project_root.iterdir() if p.name != ".gitup"]
        for item in current_items:
            safe_move(item, seed_dir / item.name)
            moved_seed = True

        print_info("GitHubから初回取得しています。")
        cp = subprocess.run(
            ["git", "clone", "--branch", branch, "--single-branch", cfg.remote_url, str(clone_dir)],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            env=git_env(),
        )
        if cp.returncode != 0:
            raise RuntimeError(cp.stderr.strip() or cp.stdout.strip() or "git clone failed")

        for item in clone_dir.iterdir():
            if item.name == ".gitup":
                continue
            dst = project_root / item.name
            if dst.exists() or dst.is_symlink():
                safe_move(dst, backup / "preexisting_after_clone" / item.name)
            shutil.move(str(item), str(dst))

        run_git(project_root, ["remote", "set-url", DEFAULT_REMOTE, cfg.remote_url])
        run_git(project_root, ["checkout", branch])
        run_git(project_root, ["branch", "--set-upstream-to", remote_branch_ref(branch), branch])
        ensure_gitignore(project_root, load_config(project_root))
        print_info("初回取得が完了しました。seedファイルは .gitup/backups に退避済みです。")
        log_event(project_root, "bootstrap completed")
        if not no_restart:
            restart_official(project_root)
        return True
    except Exception as exc:
        print_error("bootstrapに失敗しました。seed状態を復元します。", str(exc))
        if moved_seed:
            failed_dir.mkdir(parents=True, exist_ok=True)
            for item in list(project_root.iterdir()):
                if item.name == ".gitup":
                    continue
                safe_move(item, failed_dir / item.name)
            for item in list(seed_dir.iterdir()):
                safe_move(item, project_root / item.name)
        log_event(project_root, f"bootstrap failed: {exc}")
        return False
    finally:
        shutil.rmtree(temp_parent, ignore_errors=True)


def restart_official(project_root: Path) -> None:
    try:
        exe = project_root / "GitUp Manager.exe"
        script = project_root / "_gitup.py"
        if exe.exists():
            subprocess.Popen([str(exe)], cwd=str(project_root))
        elif script.exists():
            subprocess.Popen([sys.executable, str(script)], cwd=str(project_root))
        print_info("取得した正式版のGitUp Managerを起動しました。")
    except Exception as exc:
        print_warn(f"正式版の再起動に失敗しました: {exc}")


def rebuild_from_github_flow(
    project_root: Path,
    cfg: AppConfig,
    branch: Optional[str] = None,
    no_restart: bool = False,
    non_interactive: bool = False,
) -> bool:
    remote_url = cfg.remote_url
    if inside_git_repo(project_root):
        cp = run_git(project_root, ["remote", "get-url", DEFAULT_REMOTE])
        if cp.returncode == 0 and cp.stdout.strip():
            remote_url = cp.stdout.strip()
    if not remote_url:
        if non_interactive:
            print_error("remote_urlが未設定です。")
            return False
        remote_url = input_default("GitHub URL", cfg.remote_url)
        cfg.remote_url = remote_url
        save_config(project_root, cfg)
    if not remote_url:
        print_error("GitHub URLが未設定です。")
        return False

    target_branch = branch or get_branch(project_root) or branch_default(cfg, pull=True)
    if not non_interactive:
        target_branch = input_default("GitHubからコピーするブランチ", target_branch)
    if not target_branch:
        target_branch = DEFAULT_BRANCH

    print_warn("現在フォルダの内容をGitHub版で完全に作り直します。")
    print_warn("現在の内容は .gitup/backups に退避しますが、作業ツリーはGitHub版へ置き換わります。")
    if not confirm("実行しますか？", default=False, non_interactive=non_interactive):
        print_info("キャンセルしました。")
        return False

    ensure_runtime_dirs(project_root)
    backup = backups_dir(project_root) / f"{now_stamp()}_rebuild_from_github"
    current_dir = backup / "current"
    failed_dir = backup / "failed_partial"
    current_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "operation": "rebuild_from_github",
        "remote_url": remote_url,
        "branch": target_branch,
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "project_root": str(project_root),
        "previous_head": get_head(project_root) if inside_git_repo(project_root) else "",
    }
    (backup / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    temp_parent = Path(tempfile.mkdtemp(prefix="gitup_rebuild_"))
    clone_dir = temp_parent / "repo"
    moved_current = False
    try:
        print_info("GitHub版を一時フォルダへcloneしています。")
        cp = subprocess.run(
            ["git", "clone", "--branch", target_branch, "--single-branch", remote_url, str(clone_dir)],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            env=git_env(),
        )
        if cp.returncode != 0:
            raise RuntimeError(cp.stderr.strip() or cp.stdout.strip() or "git clone failed")

        for item in list(project_root.iterdir()):
            if item.name == ".gitup":
                continue
            safe_move(item, current_dir / item.name)
            moved_current = True

        for item in list(clone_dir.iterdir()):
            if item.name == ".gitup":
                continue
            shutil.move(str(item), str(project_root / item.name))

        loaded_cfg = load_config(project_root)
        if loaded_cfg.remote_url != remote_url:
            loaded_cfg.remote_url = remote_url
            save_config(project_root, loaded_cfg)
        if inside_git_repo(project_root):
            run_git(project_root, ["remote", "set-url", DEFAULT_REMOTE, remote_url])
            run_git(project_root, ["checkout", target_branch])
            run_git(project_root, ["branch", "--set-upstream-to", remote_branch_ref(target_branch), target_branch])

        print_info("GitHub版で完全に作り直しました。以前の内容はバックアップに退避済みです。")
        print_info(f"バックアップ: {backup}")
        log_event(project_root, f"rebuild_from_github completed branch={target_branch}")
        if not no_restart:
            restart_official(project_root)
        return True
    except Exception as exc:
        print_error("GitHub版での作り直しに失敗しました。可能な範囲で元の内容を復元します。", str(exc))
        if moved_current:
            failed_dir.mkdir(parents=True, exist_ok=True)
            for item in list(project_root.iterdir()):
                if item.name == ".gitup":
                    continue
                safe_move(item, failed_dir / item.name)
            for item in list(current_dir.iterdir()):
                safe_move(item, project_root / item.name)
        log_event(project_root, f"rebuild_from_github failed: {exc}")
        return False
    finally:
        shutil.rmtree(temp_parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# OpenAI key storage and model routing
# ---------------------------------------------------------------------------


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


class WindowsCredentialStore:
    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    def __init__(self) -> None:
        self.advapi32 = ctypes.windll.advapi32  # type: ignore[attr-defined]
        self.advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), ctypes.c_uint32]
        self.advapi32.CredWriteW.restype = ctypes.c_bool
        self.advapi32.CredReadW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        self.advapi32.CredReadW.restype = ctypes.c_bool
        self.advapi32.CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
        self.advapi32.CredDeleteW.restype = ctypes.c_bool
        self.advapi32.CredFree.argtypes = [ctypes.c_void_p]

    def write(self, target: str, username: str, secret: str) -> None:
        blob = secret.encode("utf-16-le")
        if len(blob) > 2560:
            raise ValueError("APIキーがWindows資格情報の上限を超えています。")
        buf = ctypes.create_string_buffer(blob)
        cred = _CREDENTIAL()
        cred.Type = self.CRED_TYPE_GENERIC
        cred.TargetName = target
        cred.UserName = username
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        if not self.advapi32.CredWriteW(ctypes.byref(cred), 0):
            raise ctypes.WinError()  # type: ignore[attr-defined]

    def read(self, target: str) -> Optional[str]:
        ptr = ctypes.c_void_p()
        if not self.advapi32.CredReadW(target, self.CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
            return None
        try:
            cred = ctypes.cast(ptr, ctypes.POINTER(_CREDENTIAL)).contents
            raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
            for encoding in ("utf-16-le", "utf-8"):
                try:
                    return raw.decode(encoding).rstrip("\x00")
                except UnicodeDecodeError:
                    continue
            return None
        finally:
            self.advapi32.CredFree(ptr)

    def delete(self, target: str) -> bool:
        return bool(self.advapi32.CredDeleteW(target, self.CRED_TYPE_GENERIC, 0))


def read_openai_key() -> Optional[str]:
    if is_windows():
        try:
            return WindowsCredentialStore().read(OPENAI_CREDENTIAL_TARGET)
        except Exception:
            return None
    return os.environ.get("OPENAI_API_KEY")


def write_openai_key(secret: str) -> None:
    if not is_windows():
        raise RuntimeError("Windows以外ではOPENAI_API_KEY環境変数を使ってください。")
    WindowsCredentialStore().write(OPENAI_CREDENTIAL_TARGET, OPENAI_CREDENTIAL_USER, secret)


def delete_openai_key() -> bool:
    if not is_windows():
        return False
    try:
        return WindowsCredentialStore().delete(OPENAI_CREDENTIAL_TARGET)
    except Exception:
        return False


def key_fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8", errors="replace")).hexdigest()[:8]


def model_for(cfg: AppConfig, task: str) -> str:
    return cfg.ai_models.get(task) or DEFAULT_AI_MODELS.get(task) or "gpt-5.4-mini"


def reasoning_for(cfg: AppConfig, task: str) -> str:
    return cfg.ai_reasoning_effort.get(task) or DEFAULT_REASONING.get(task) or "low"


def extract_response_text(response: object) -> str:
    text = getattr(response, "output_text", None)
    if isinstance(text, str):
        return " ".join(text.split())
    parts: List[str] = []
    output = getattr(response, "output", None)
    if isinstance(output, list):
        for item in output:
            content = getattr(item, "content", None)
            if isinstance(content, list):
                for part in content:
                    value = getattr(part, "text", None)
                    if isinstance(value, str):
                        parts.append(value)
    return " ".join(" ".join(parts).split())


def openai_commit_summary(cfg: AppConfig, prompt: str, fallback: str) -> str:
    if not cfg.ai_enabled or cfg.ai_provider != "openai":
        return fallback
    if OpenAI is None:
        return fallback
    key = read_openai_key()
    if not key:
        return fallback
    try:
        client = OpenAI(api_key=key)
        model = model_for(cfg, "commit_summary")
        reasoning = reasoning_for(cfg, "commit_summary")
        if cfg.ai_api == "responses" and hasattr(client, "responses"):
            response = client.responses.create(
                model=model,
                input=[
                    {
                        "role": "system",
                        "content": "日本語のGitコミット文を1行で作る。70文字前後。秘密情報らしき文字列は書かない。",
                    },
                    {"role": "user", "content": prompt},
                ],
                reasoning={"effort": reasoning},
                max_output_tokens=220,
            )
            text = extract_response_text(response)
        else:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "日本語のGitコミット文を1行で作る。秘密情報らしき文字列は書かない。"},
                    {"role": "user", "content": prompt},
                ],
                max_completion_tokens=220,
                reasoning_effort=reasoning,
            )
            text = response.choices[0].message.content or ""
        text = " ".join(mask_secrets(text).split())
        return text[:180] if text else fallback
    except Exception:
        return fallback


def ai_settings_menu(project_root: Path, cfg: AppConfig) -> AppConfig:
    while True:
        print("AI / APIキー設定")
        print("[1] APIキーを登録・更新")
        print("[2] APIキー登録状態を確認")
        print("[3] APIキーを削除")
        print("[4] AIモデル設定を変更")
        print("[5] AI機能を無効化")
        print("[6] 戻る")
        choice = input("番号: ").strip()
        if choice == "1":
            if not is_windows():
                print_warn("Windows以外ではOPENAI_API_KEY環境変数を使用します。")
                continue
            secret = getpass.getpass("OpenAI API key: ").strip()
            if not secret:
                print_warn("空のキーは保存しません。")
                continue
            write_openai_key(secret)
            print_info(f"APIキーをWindows資格情報へ保存しました。fingerprint={key_fingerprint(secret)}")
        elif choice == "2":
            secret = read_openai_key()
            if secret:
                print_info(f"APIキーは登録済みです。fingerprint={key_fingerprint(secret)}")
            else:
                print_warn("APIキーは未登録です。AIなしでもpushできます。")
        elif choice == "3":
            if delete_openai_key():
                print_info("APIキーを削除しました。")
            else:
                print_warn("APIキーは見つからないか、削除できませんでした。")
        elif choice == "4":
            print("[1] economy")
            print("[2] balanced")
            print("[3] quality")
            sel = input("番号: ").strip()
            mode = {"1": "economy", "2": "balanced", "3": "quality"}.get(sel, "balanced")
            cfg.ai_mode = mode
            cfg.ai_models = AI_PRESETS[mode].copy()
            cfg.ai_enabled = True
            save_config(project_root, cfg)
            print_info(f"AIモデル設定を {mode} に変更しました。")
        elif choice == "5":
            cfg.ai_enabled = False
            save_config(project_root, cfg)
            print_info("AI機能を無効化しました。")
        elif choice == "6":
            return cfg


# ---------------------------------------------------------------------------
# Pull/push
# ---------------------------------------------------------------------------


def fetch_origin(project_root: Path, branch: Optional[str] = None) -> bool:
    args = ["fetch", DEFAULT_REMOTE]
    if branch:
        args.append(branch)
    cp = run_git(project_root, args)
    if cp.returncode != 0:
        print_error("GitHubから情報取得できませんでした。", cp.stderr)
        return False
    return True


def checkout_branch(project_root: Path, branch: str, start_point: Optional[str] = None) -> bool:
    cp = run_git(project_root, ["rev-parse", "--verify", branch])
    if cp.returncode == 0:
        res = run_git(project_root, ["checkout", branch])
    elif start_point:
        res = run_git(project_root, ["checkout", "-b", branch, start_point])
    else:
        res = run_git(project_root, ["checkout", "-b", branch])
    if res.returncode != 0:
        print_error("ブランチ切り替えに失敗しました。", res.stderr)
        return False
    return True


def set_upstream_if_needed(project_root: Path, branch: str) -> None:
    if get_upstream(project_root):
        return
    run_git(project_root, ["branch", "--set-upstream-to", remote_branch_ref(branch), branch])


def stash_if_dirty(project_root: Path, operation: str) -> bool:
    doctor = diagnose(project_root)
    if not doctor.dirty:
        return False
    create_backup(project_root, operation)
    cp = run_git(project_root, ["stash", "push", "--include-untracked", "-m", f"GitUp {operation} {now_stamp()}"])
    if cp.returncode != 0:
        print_error("変更の一時退避に失敗しました。", cp.stderr)
        return False
    if "No local changes" in cp.stdout:
        return False
    print_info("このPCの変更を一時退避しました。")
    return True


def pop_stash(project_root: Path) -> bool:
    create_backup(project_root, "stash_pop")
    cp = run_git(project_root, ["stash", "pop"])
    if cp.returncode != 0:
        print_warn("一時退避した変更の復元で衝突しました。Conflict Wizardを開始します。")
        conflict_wizard(project_root)
        return False
    print_info("一時退避した変更を戻しました。")
    return True


def safe_pull(project_root: Path, cfg: AppConfig, branch: Optional[str] = None, non_interactive: bool = False) -> bool:
    if not inside_git_repo(project_root):
        return bootstrap_flow(project_root, cfg, non_interactive=non_interactive)
    ensure_gitignore(project_root, cfg)
    ensure_identity(project_root, non_interactive=non_interactive)
    if not ensure_remote(project_root, cfg, non_interactive=non_interactive):
        return False

    target = branch or get_branch(project_root) or branch_default(cfg, pull=True)
    if not target:
        target = DEFAULT_BRANCH
    if not checkout_branch(project_root, target, start_point=remote_branch_ref(target) if get_head(project_root, remote_branch_ref(target)) else None):
        return False
    if not fetch_origin(project_root, target):
        return False

    set_upstream_if_needed(project_root, target)
    before_head = get_head(project_root)
    stashed = stash_if_dirty(project_root, "pull")
    doctor = diagnose(project_root)
    remote_ref = remote_branch_ref(target)
    if not doctor.upstream:
        doctor.upstream = remote_ref
    doctor.ahead, doctor.behind = ahead_behind(project_root, doctor.upstream)
    doctor.diverged = doctor.ahead > 0 and doctor.behind > 0

    ok = True
    if doctor.behind == 0 and not doctor.diverged:
        print_info("GitHub版の新しい更新はありません。")
    elif doctor.ahead == 0:
        create_backup(project_root, "pull_ff")
        cp = run_git(project_root, ["merge", "--ff-only", remote_ref])
        if cp.returncode != 0:
            print_error("fast-forward更新に失敗しました。", cp.stderr)
            ok = False
        else:
            print_info("GitHub版を取り込みました。")
    else:
        create_backup(project_root, "pull_rebase")
        use_rebase = True
        if not non_interactive:
            print_warn("このPCとGitHubの履歴が分かれています。")
            print("[1] safe rebase (推奨)")
            print("[2] merge")
            use_rebase = (input("番号 [1]: ").strip() or "1") != "2"
        cp = run_git(project_root, ["rebase", remote_ref] if use_rebase else ["merge", "--no-edit", remote_ref])
        if cp.returncode != 0:
            print_warn("取り込み中に衝突しました。Conflict Wizardを開始します。")
            conflict_wizard(project_root, default_choice=None if not non_interactive else "local")
            ok = not conflict_paths(project_root)
        else:
            print_info("分岐した履歴を安全に取り込みました。")

    if stashed:
        pop_stash(project_root)
        ok = ok and not conflict_paths(project_root)

    after_head = get_head(project_root)
    if before_head and after_head and before_head != after_head:
        changed = run_git(project_root, ["diff", "--name-only", f"{before_head}..{after_head}"]).stdout.splitlines()
        if any(is_system_path(p) for p in changed):
            print_warn("_gitup.py または GitUp Manager.exe が更新されました。完了後にGitUp Managerを再起動してください。")

    if ok:
        state = load_state(project_root)
        state["last_successful_pull"] = _dt.datetime.now().isoformat(timespec="seconds")
        state["last_selected_branch"] = target
        save_state(project_root, state)
        print_info("Pull完了。")
    return ok


def paths_for_stage(change: GitChange) -> List[str]:
    paths = []
    if change.old_path:
        paths.append(change.old_path)
    if change.path:
        paths.append(change.path)
    return paths


def stage_changes(
    project_root: Path,
    cfg: AppConfig,
    include_system: bool = False,
) -> Tuple[List[str], List[str], List[str]]:
    changes = get_changes(project_root)
    staged: List[str] = []
    skipped: List[str] = []
    system: List[str] = []
    for ch in changes:
        if ch.is_conflict:
            continue
        p = ch.path
        if path_matches(p, cfg.exclude):
            skipped.append(p)
            continue
        if is_system_path(p):
            system.append(p)
            if not include_system:
                skipped.append(p)
                continue
        elif path_matches(p, cfg.stage_protected):
            skipped.append(p)
            continue
        args = ["add", "-A", "--", *paths_for_stage(ch)]
        cp = run_git(project_root, args)
        if cp.returncode == 0:
            staged.append(p)
        else:
            skipped.append(p)
    return sorted(set(staged)), sorted(set(skipped)), sorted(set(system))


def staged_summary(project_root: Path, cfg: AppConfig) -> Tuple[str, str]:
    numstat = run_git(project_root, ["diff", "--cached", "--numstat"]).stdout
    name_status = run_git(project_root, ["diff", "--cached", "--name-status"]).stdout
    stat = run_git(project_root, ["diff", "--cached", "--stat"]).stdout
    diff = run_git(project_root, ["diff", "--cached", "--", ":!*.png", ":!*.jpg", ":!*.jpeg", ":!*.gif", ":!*.pdf", ":!*.exe"]).stdout
    diff = mask_secrets(diff[:MAX_AI_DIFF_CHARS])
    prompt = (
        "次のステージ済み変更からコミット文を作ってください。\n"
        "ファイル一覧:\n"
        f"{mask_secrets(name_status)}\n"
        "numstat:\n"
        f"{mask_secrets(numstat)}\n"
        "stat:\n"
        f"{mask_secrets(stat)}\n"
        "短いdiff抜粋:\n"
        f"{diff}"
    )
    files = [line for line in name_status.splitlines() if line.strip()]
    fallback = f"chore: {len(files)}件の変更を更新"
    return prompt, fallback


def manual_commit_message(default: str, non_interactive: bool = False) -> str:
    if non_interactive:
        return default
    value = input_default("コミット文", default)
    return value or default


def push_existing_commits(project_root: Path, target: str) -> bool:
    push_args = ["push"] if get_upstream(project_root) else ["push", "--set-upstream", DEFAULT_REMOTE, target]
    cp = run_git(project_root, push_args)
    if cp.returncode != 0:
        print_error("pushに失敗しました。force pushは通常メニューでは実行しません。", cp.stderr)
        return False
    state = load_state(project_root)
    state["last_successful_push"] = _dt.datetime.now().isoformat(timespec="seconds")
    state["last_selected_branch"] = target
    save_state(project_root, state)
    print_info("Push完了。")
    return True


def safe_push(project_root: Path, cfg: AppConfig, branch: Optional[str] = None, non_interactive: bool = False) -> bool:
    if not inside_git_repo(project_root):
        print_warn("まだGitHubから初回取得されていません。先に初回取得を実行します。")
        return bootstrap_flow(project_root, cfg, non_interactive=non_interactive)
    ensure_gitignore(project_root, cfg)
    ensure_identity(project_root, non_interactive=non_interactive)
    if not ensure_remote(project_root, cfg, non_interactive=non_interactive):
        return False

    target = branch or get_branch(project_root) or branch_default(cfg, pull=False)
    if not checkout_branch(project_root, target):
        return False
    if not fetch_origin(project_root, target):
        return False
    set_upstream_if_needed(project_root, target)

    doctor = diagnose(project_root)
    if doctor.conflicts:
        print_warn("未解消の衝突があります。先に衝突を解消してください。")
        conflict_wizard(project_root)
        return False
    if doctor.behind > 0:
        print_warn("GitHub版が先行しています。先に取り込みます。")
        if non_interactive or confirm("safe pullを実行しますか？", default=True):
            if not safe_pull(project_root, cfg, branch=target, non_interactive=non_interactive):
                return False
        else:
            return False

    changes = get_changes(project_root)
    if not changes:
        doctor = diagnose(project_root)
        if doctor.ahead <= 0:
            print_info("pushする変更はありません。")
            return True
        return push_existing_commits(project_root, target)

    system_changes = sorted({ch.path for ch in changes if is_system_path(ch.path)})
    project_changes = sorted({ch.path for ch in changes if not is_system_path(ch.path)})
    print("変更ファイル:")
    for p in project_changes:
        print(f"  [project] {p}")
    for p in system_changes:
        print(f"  [system]  {p}")

    include_system = False
    if system_changes and not non_interactive:
        include_system = confirm("system files updateとしてGitUp管理ファイルも含めますか？", default=False)
    create_backup(project_root, "push")
    staged, skipped, system = stage_changes(project_root, cfg, include_system=include_system)
    if skipped:
        print("stage対象外:")
        for p in skipped:
            print(f"  - {p}")
    if staged:
        print("stage対象:")
        for p in staged:
            print(f"  - {p}")

    if run_git(project_root, ["diff", "--cached", "--quiet"]).returncode == 0:
        doctor = diagnose(project_root)
        if doctor.ahead > 0:
            print_info("stage対象はありませんが、未pushのコミットを送信します。")
            return push_existing_commits(project_root, target)
        print_info("stageされた変更がありません。")
        return True

    prompt, fallback = staged_summary(project_root, cfg)
    ai_msg = openai_commit_summary(cfg, prompt, fallback)
    commit_msg = ai_msg if ai_msg.startswith(("fix:", "feat:", "chore:", "docs:", "refactor:", "test:", "build:")) else f"chore: {ai_msg}"
    print(f"コミット文候補: {commit_msg}")
    if not non_interactive:
        if not confirm("このコミット文で進めますか？", default=True):
            commit_msg = manual_commit_message(fallback, non_interactive=False)

    cp = run_git(project_root, ["commit", "-m", commit_msg])
    if cp.returncode != 0:
        print_error("commitに失敗しました。", cp.stderr)
        return False
    push_args = ["push"]
    if not get_upstream(project_root):
        push_args = ["push", "--set-upstream", DEFAULT_REMOTE, target]
    cp = run_git(project_root, push_args)
    if cp.returncode != 0:
        print_error("pushに失敗しました。GitHub版が先行している可能性があります。force pushは通常メニューでは実行しません。", cp.stderr)
        print_info("PullでGitHub版を取り込んでから、もう一度Pushしてください。")
        return False

    state = load_state(project_root)
    state["last_successful_push"] = _dt.datetime.now().isoformat(timespec="seconds")
    state["last_selected_branch"] = target
    save_state(project_root, state)
    print_info("Push完了。")
    return True


def force_with_lease_flow(project_root: Path, cfg: AppConfig) -> None:
    doctor = diagnose(project_root)
    if not doctor.is_repo or not doctor.branch:
        print_warn("Gitリポジトリまたはブランチがありません。")
        return
    fetch_origin(project_root, doctor.branch)
    remote_ref = remote_branch_ref(doctor.branch)
    remote_hash = get_head(project_root, remote_ref)
    print_warn("--force-with-lease は高度設定です。他PCの履歴を上書きしないための確認付きforceです。")
    print(f"対象branch: {doctor.branch}")
    print(f"remote hash: {remote_hash or '(なし)'}")
    if confirm("本当に --force-with-lease を実行しますか？", default=False):
        create_backup(project_root, "force_with_lease")
        cp = run_git(project_root, ["push", "--force-with-lease", DEFAULT_REMOTE, doctor.branch])
        if cp.returncode != 0:
            print_error("force-with-leaseに失敗しました。", cp.stderr)
        else:
            print_info("force-with-leaseが完了しました。")


# ---------------------------------------------------------------------------
# Conflict Wizard
# ---------------------------------------------------------------------------


@dataclass
class ConflictInfo:
    path: str
    stages: Dict[int, str]
    kind: str
    binary: bool


def read_conflict_stages(project_root: Path, path: str) -> Dict[int, str]:
    cp = run_git_bytes(project_root, ["ls-files", "-u", "-z", "--", path])
    stages: Dict[int, str] = {}
    if cp.returncode != 0:
        return stages
    for raw in cp.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            meta, _ = raw.split(b"\t", 1)
            mode, sha, stage = meta.split(b" ")
            stages[int(stage)] = decode_path(sha)
        except Exception:
            continue
    return stages


def blob_bytes(project_root: Path, stage: int, path: str) -> Optional[bytes]:
    cp = subprocess.run(
        ["git", "show", f":{stage}:{path}"],
        cwd=str(project_root),
        capture_output=True,
        text=False,
        env=git_env(),
    )
    if cp.returncode != 0:
        return None
    return cp.stdout


def is_binary_content(data: Optional[bytes]) -> bool:
    if data is None:
        return False
    return b"\0" in data[:4096]


def conflict_info(project_root: Path, path: str) -> ConflictInfo:
    stages = read_conflict_stages(project_root, path)
    has2 = 2 in stages
    has3 = 3 in stages
    if has2 and has3 and 1 not in stages:
        kind = "both added"
    elif has2 and not has3:
        kind = "deleted by GitHub版" if not is_rebase_in_progress(project_root) else "deleted by このPC版"
    elif has3 and not has2:
        kind = "deleted by このPC版" if not is_rebase_in_progress(project_root) else "deleted by GitHub版"
    else:
        kind = "text"
    b2 = blob_bytes(project_root, 2, path)
    b3 = blob_bytes(project_root, 3, path)
    binary = is_binary_content(b2) or is_binary_content(b3)
    if binary:
        kind = "binary"
    return ConflictInfo(path=path, stages=stages, kind=kind, binary=binary)


def is_rebase_in_progress(project_root: Path) -> bool:
    return git_path(project_root, "rebase-merge").exists() or git_path(project_root, "rebase-apply").exists()


def stage_for_side(project_root: Path, side: str) -> int:
    rebase = is_rebase_in_progress(project_root)
    if side == "local":
        return 3 if rebase else 2
    return 2 if rebase else 3


def label_for_stage(project_root: Path, stage: int) -> str:
    rebase = is_rebase_in_progress(project_root)
    if rebase:
        return "GitHub版" if stage == 2 else "このPC版"
    return "このPC版" if stage == 2 else "GitHub版"


def checkout_conflict_side(project_root: Path, path: str, side: str) -> bool:
    stage = stage_for_side(project_root, side)
    data = blob_bytes(project_root, stage, path)
    target = project_root / path
    if data is None:
        cp = run_git(project_root, ["rm", "-f", "--", path])
        return cp.returncode == 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    cp = run_git(project_root, ["add", "--", path])
    return cp.returncode == 0


def side_copy_name(path: str, label: str) -> str:
    p = Path(path)
    suffix = ".local" if label == "このPC版" else ".remote"
    return str(p.with_name(p.stem + suffix + p.suffix)).replace("\\", "/")


def keep_both_conflict(project_root: Path, path: str) -> bool:
    wrote = False
    for stage in (stage_for_side(project_root, "local"), stage_for_side(project_root, "remote")):
        data = blob_bytes(project_root, stage, path)
        if data is None:
            continue
        label = label_for_stage(project_root, stage)
        out_rel = side_copy_name(path, label)
        out = project_root / out_rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        run_git(project_root, ["add", "--", out_rel])
        wrote = True
    if wrote:
        checkout_conflict_side(project_root, path, "local")
    return wrote


def show_conflict_diff(project_root: Path, path: str) -> None:
    local = blob_bytes(project_root, stage_for_side(project_root, "local"), path)
    remote = blob_bytes(project_root, stage_for_side(project_root, "remote"), path)
    if is_binary_content(local) or is_binary_content(remote):
        print_warn("binaryファイルのため差分表示はできません。")
        return
    local_text = (local or b"").decode("utf-8", errors="replace").splitlines()
    remote_text = (remote or b"").decode("utf-8", errors="replace").splitlines()
    diff = difflib.unified_diff(local_text, remote_text, fromfile="このPC版", tofile="GitHub版", lineterm="")
    for i, line in enumerate(diff):
        if i > 200:
            print("... (省略)")
            break
        print(line)


def continue_pending_operation(project_root: Path) -> None:
    env = {"GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"}
    if is_rebase_in_progress(project_root):
        cp = run_git(project_root, ["rebase", "--continue"], extra_env=env)
        if cp.returncode != 0:
            print_warn("rebase --continue が完了しませんでした。復旧メニューを確認してください。")
            print(mask_secrets(cp.stderr))
    elif git_path(project_root, "MERGE_HEAD").exists():
        cp = run_git(project_root, ["commit", "--no-edit"], extra_env=env)
        if cp.returncode != 0:
            print_warn("merge commitが完了しませんでした。")
            print(mask_secrets(cp.stderr))
    elif git_path(project_root, "CHERRY_PICK_HEAD").exists():
        cp = run_git(project_root, ["cherry-pick", "--continue"], extra_env=env)
        if cp.returncode != 0:
            print_warn("cherry-pick --continue が完了しませんでした。")
            print(mask_secrets(cp.stderr))


def conflict_wizard(project_root: Path, default_choice: Optional[str] = None) -> bool:
    conflicts = conflict_paths(project_root)
    if not conflicts:
        print_info("未解消の衝突はありません。")
        return True
    create_backup(project_root, "conflict_resolution", extra_paths=conflicts)
    for path in conflicts:
        info = conflict_info(project_root, path)
        while True:
            print("")
            print(f"衝突ファイル: {path}")
            print(f"種類: {info.kind}")
            if default_choice in {"local", "remote"}:
                choice = "1" if default_choice == "local" else "2"
            else:
                print("[1] このPC版を採用")
                print("[2] GitHub版を採用")
                print("[3] 両方残す")
                print("[4] 差分を見る")
                print("[5] 後で処理")
                print("[6] 中止して復元")
                choice = input("番号: ").strip()
            if choice == "1":
                if checkout_conflict_side(project_root, path, "local"):
                    print_info("このPC版を採用しました。")
                    break
            elif choice == "2":
                if checkout_conflict_side(project_root, path, "remote"):
                    print_info("GitHub版を採用しました。")
                    break
            elif choice == "3":
                if keep_both_conflict(project_root, path):
                    print_info("両方の版を別ファイルとして残しました。")
                    break
            elif choice == "4":
                show_conflict_diff(project_root, path)
            elif choice == "5":
                print_warn("このファイルは未解消のまま残します。")
                break
            elif choice == "6":
                recovery_menu(project_root)
                return False
            else:
                print("番号を選んでください。")
            if default_choice:
                break

    remaining = conflict_paths(project_root)
    if remaining:
        print_warn("未解消の衝突が残っています。")
        for p in remaining:
            print(f"  - {p}")
        return False
    continue_pending_operation(project_root)
    print_info("衝突解消が完了しました。")
    return True


# ---------------------------------------------------------------------------
# Cleanup/build/diagnostics
# ---------------------------------------------------------------------------


def cleanup_candidates(project_root: Path) -> List[Path]:
    candidates: List[Path] = []
    fixed = [
        "build",
        "dist",
        "__pycache__",
        ".pytest_cache",
        "_bootstrap.py",
        "_bootstrap_patch_helper.txt",
        "release/manifest.json",
        "release/latest/main.exe",
        "release/latest/.gitkeep",
        "release/.gitkeep",
    ]
    for item in fixed:
        p = project_root / item
        if p.exists():
            candidates.append(p)
    candidates.extend(project_root.glob("*.spec"))

    runtime = gitup_dir(project_root) / "runtime"
    if runtime.exists():
        candidates.extend([p for p in runtime.iterdir() if p.is_dir()])

    cutoff = time.time() - 30 * 24 * 60 * 60
    for root in (logs_dir(project_root), backups_dir(project_root)):
        if root.exists():
            for p in root.iterdir():
                try:
                    if p.stat().st_mtime < cutoff:
                        candidates.append(p)
                except OSError:
                    pass
    return sorted(set(candidates), key=lambda p: str(p).lower())


def cleanup_flow(project_root: Path, dry_run: bool = False, non_interactive: bool = False) -> None:
    candidates = cleanup_candidates(project_root)
    if not candidates:
        print_info("整理対象はありません。")
        return
    print("整理候補:")
    for p in candidates:
        print(f"  - {rel_path(project_root, p)}")
    if dry_run:
        print_info("dry-runのため削除しません。")
        return
    if not confirm("上記をバックアップ後に削除しますか？", default=False, non_interactive=non_interactive):
        return
    create_backup(project_root, "system_cleanup", extra_paths=[rel_path(project_root, p) for p in candidates])
    for p in candidates:
        if p.name == "main.py":
            continue
        if not path_is_relative_to(p, project_root):
            continue
        safe_remove(p)
    print_info("不要ファイル整理が完了しました。")


def build_exe(project_root: Path, cfg: AppConfig, extra_opts: Optional[List[str]] = None) -> Optional[Path]:
    entry = project_root / "_gitup.py"
    if not entry.exists():
        print_error("_gitup.py が見つかりません。")
        return None
    create_backup(project_root, "build_exe", extra_paths=["GitUp Manager.exe"])
    dist = project_root / "dist"
    build = project_root / "build"
    dist.mkdir(exist_ok=True)
    build.mkdir(exist_ok=True)
    exe_base = Path(cfg.exe_name).stem or "GitUp Manager"
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--clean",
        "--noconfirm",
        "--name",
        exe_base,
        "--distpath",
        str(dist),
        "--workpath",
        str(build),
        "--specpath",
        str(build),
        str(entry),
    ]
    if extra_opts:
        cmd.extend(extra_opts)
    print_info("PyInstallerでGitUp Manager.exeを作成します。")
    cp = subprocess.run(cmd, cwd=str(project_root), text=True, encoding="utf-8", errors="replace")
    if cp.returncode != 0:
        print_error("PyInstaller buildに失敗しました。")
        return None
    built = dist / f"{exe_base}.exe"
    if not built.exists():
        print_error("buildは終了しましたがexeが見つかりません。")
        return None
    target = project_root / "GitUp Manager.exe"
    if target.exists():
        target.unlink()
    shutil.copy2(built, target)
    print_info(f"作成しました: {target}")
    return target


def build_flow(project_root: Path, cfg: AppConfig) -> None:
    additional = input_default("追加PyInstallerオプション（空でなし）", "")
    opts = additional.split() if additional else []
    build_exe(project_root, cfg, opts)


def diagnostics_menu(project_root: Path, cfg: AppConfig) -> None:
    while True:
        doctor = diagnose(project_root, cfg)
        print_doctor(project_root, cfg, doctor)
        print("")
        print("[1] 詳細statusを表示")
        print("[2] remote originを設定/更新")
        print("[3] upstreamを現在branchに設定")
        print("[4] 高度: --force-with-lease")
        print("[5] GitHub版で完全に作り直す")
        print("[6] 戻る")
        choice = input("番号: ").strip()
        if choice == "1":
            cp = run_git(project_root, ["status", "--short", "--branch"])
            print(mask_secrets(cp.stdout + cp.stderr))
        elif choice == "2":
            cfg.remote_url = input_default("remote_url", cfg.remote_url)
            save_config(project_root, cfg)
            if inside_git_repo(project_root):
                if run_git(project_root, ["remote", "get-url", DEFAULT_REMOTE]).returncode == 0:
                    run_git(project_root, ["remote", "set-url", DEFAULT_REMOTE, cfg.remote_url])
                else:
                    run_git(project_root, ["remote", "add", DEFAULT_REMOTE, cfg.remote_url])
        elif choice == "3":
            branch = get_branch(project_root)
            if branch:
                run_git(project_root, ["branch", "--set-upstream-to", remote_branch_ref(branch), branch])
        elif choice == "4":
            force_with_lease_flow(project_root, cfg)
        elif choice == "5":
            rebuild_from_github_flow(project_root, cfg)
        elif choice == "6":
            return


# ---------------------------------------------------------------------------
# Menus/CLI
# ---------------------------------------------------------------------------


def configure_flow(project_root: Path, cfg: AppConfig) -> AppConfig:
    cfg.app_name = input_default("app.name", cfg.app_name)
    cfg.exe_name = input_default("app.exe_name", cfg.exe_name)
    cfg.remote_url = input_default("remote_url", cfg.remote_url)
    cfg.stable_branch = input_default("branches.stable", cfg.stable_branch)
    cfg.develop_branch = input_default("branches.develop", cfg.develop_branch)
    cfg.default_pull = input_default("branches.default_pull", cfg.default_pull)
    cfg.default_push = input_default("branches.default_push", cfg.default_push)
    save_config(project_root, cfg)
    ensure_gitignore(project_root, cfg)
    print_info("設定を保存しました。")
    return cfg


def prompt_branch(cfg: AppConfig, pull: bool) -> str:
    default = branch_default(cfg, pull=pull)
    print("[1] stable")
    print("[2] develop")
    print("[3] 現在のbranch")
    choice = input("番号 [1]: ").strip() or "1"
    if choice == "2":
        return cfg.develop_branch
    if choice == "3":
        return ""
    return default


def main_menu(project_root: Path, cfg: AppConfig) -> None:
    ensure_runtime_dirs(project_root)
    ensure_gitignore(project_root, cfg)
    while True:
        cfg = load_config(project_root)
        doctor = diagnose(project_root, cfg)
        print("")
        print("GitUp Manager")
        print(f"version: {APP_VERSION}")
        print_doctor(project_root, cfg, doctor)
        print("")
        print("[1] おすすめ操作を実行")
        print("[2] Pull")
        print("[3] Push")
        print("[4] 衝突を解消")
        print("[5] 履歴から戻す")
        print("[6] Build GitUp Manager.exe")
        print("[7] AI / APIキー設定")
        print("[8] 診断・修復")
        print("[9] 不要ファイル整理")
        print("[0] 終了")
        choice = input("番号: ").strip()
        if choice == "1":
            if not doctor.is_repo:
                bootstrap_flow(project_root, cfg)
            elif doctor.conflicts or doctor.in_merge or doctor.in_rebase:
                conflict_wizard(project_root)
            elif doctor.behind > 0 or doctor.diverged:
                safe_pull(project_root, cfg)
            elif doctor.dirty or doctor.ahead > 0:
                safe_push(project_root, cfg)
            else:
                safe_pull(project_root, cfg)
        elif choice == "2":
            branch = prompt_branch(cfg, pull=True)
            safe_pull(project_root, cfg, branch=branch or None)
        elif choice == "3":
            branch = prompt_branch(cfg, pull=False)
            safe_push(project_root, cfg, branch=branch or None)
        elif choice == "4":
            conflict_wizard(project_root)
        elif choice == "5":
            recovery_menu(project_root)
        elif choice == "6":
            build_flow(project_root, cfg)
        elif choice == "7":
            cfg = ai_settings_menu(project_root, cfg)
        elif choice == "8":
            diagnostics_menu(project_root, cfg)
        elif choice == "9":
            dry = confirm("dry-runで候補だけ表示しますか？", default=True)
            cleanup_flow(project_root, dry_run=dry)
        elif choice == "0":
            return
        else:
            print("番号を選んでください。")
        input("Enterでメニューへ戻ります...")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GitUp Manager")
    parser.add_argument("--shadow", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--project-root", default="", help=argparse.SUPPRESS)
    parser.add_argument("--no-restart", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--non-interactive", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--doctor", action="store_true", help="診断を表示して終了")
    parser.add_argument("--bootstrap", action="store_true", help="初回取得を実行")
    parser.add_argument("--pull", nargs="?", const="", help="Pullを実行")
    parser.add_argument("--push", nargs="?", const="", help="Pushを実行")
    parser.add_argument("--cleanup-dry-run", action="store_true", help="不要ファイル整理候補を表示")
    parser.add_argument("--resolve-conflicts", choices=["local", "remote"], help="衝突を指定側で解消")
    parser.add_argument("--rebuild-from-github", nargs="?", const="", help="GitHub版で完全に作り直す")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    original_argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(original_argv)
    launch_shadow_or_exit(args, original_argv)

    project_root = Path(args.project_root or os.environ.get("GITUP_PROJECT_ROOT") or executable_path().parent).resolve()
    ensure_runtime_dirs(project_root)
    cfg = load_config(project_root)
    ensure_gitignore(project_root, cfg)

    if args.doctor:
        print_doctor(project_root, cfg, diagnose(project_root, cfg))
        return 0
    if args.bootstrap:
        return 0 if bootstrap_flow(project_root, cfg, no_restart=args.no_restart, non_interactive=args.non_interactive) else 1
    if args.pull is not None:
        return 0 if safe_pull(project_root, cfg, branch=args.pull or None, non_interactive=args.non_interactive) else 1
    if args.push is not None:
        return 0 if safe_push(project_root, cfg, branch=args.push or None, non_interactive=args.non_interactive) else 1
    if args.cleanup_dry_run:
        cleanup_flow(project_root, dry_run=True, non_interactive=True)
        return 0
    if args.resolve_conflicts:
        return 0 if conflict_wizard(project_root, default_choice=args.resolve_conflicts) else 1
    if args.rebuild_from_github is not None:
        return (
            0
            if rebuild_from_github_flow(
                project_root,
                cfg,
                branch=args.rebuild_from_github or None,
                no_restart=args.no_restart,
                non_interactive=args.non_interactive,
            )
            else 1
        )

    main_menu(project_root, cfg)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[INFO] 中断しました。")
        raise SystemExit(130)
