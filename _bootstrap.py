#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bootstrap entry for the packaged executable.

Behavior (stable channel, every run):
- Read embedded config (includes version.stable).
- Fetch release manifest from GitHub stable branch (fixed path).
- If remote version > local version: download fixed-path latest exe, replace self, restart.
- Else: run bundled main.py.

Notes:
- If an update is available, the user is prompted (interactive console) before applying it.
- Uses a safe "bat self-replace" pattern on Windows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

DEFAULT_BRANCH = "stable"
RELEASE_MANIFEST_PATH = "release/manifest.json"  # fixed
HTTP_TIMEOUT = 15

# This placeholder is replaced during build by _gitup.py
EMBEDDED_CONFIG_JSON = "__EMBEDDED_CONFIG_JSON__"


def debug_enabled() -> bool:
    return os.environ.get("GITHUBSYNC_DEBUG", "").strip() not in ("", "0", "false", "False")


def dbg(msg: str) -> None:
    if debug_enabled():
        print(f"[DEBUG] {msg}")


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def load_embedded_config() -> Dict[str, object]:
    try:
        cfg = json.loads(EMBEDDED_CONFIG_JSON)
        if isinstance(cfg, dict):
            dbg(f"Embedded config loaded.")
            return cfg
    except Exception as exc:
        dbg(f"Failed to parse embedded config: {exc}")
    return {}


def app_version(config: Dict[str, object]) -> str:
    """Return the app version embedded into this executable.

    We intentionally derive the local version from the embedded config (version.stable)
    to avoid fragile template-replacement issues.
    """
    try:
        ver = config.get("version")
        if isinstance(ver, dict):
            v = ver.get("stable")
            if v is not None:
                s = str(v).strip()
                return s if s else "0.0.0"
    except Exception:
        pass
    return "0.0.0"


def remote_url(config: Dict[str, object]) -> str:
    url = config.get("remote_url", "")
    return str(url or "").strip()


def parse_remote(url: str) -> Optional[Tuple[str, str]]:
    """
    Accepts:
      - https://github.com/owner/repo(.git)
      - git@github.com:owner/repo(.git)
      - owner/repo
    """
    s = (url or "").strip()
    if not s:
        return None
    if s.startswith("git@github.com:"):
        slug = s.split(":", 1)[1]
    elif s.startswith("https://github.com/"):
        slug = s.split("https://github.com/", 1)[1]
    elif "/" in s and "://" not in s:
        slug = s
    else:
        return None

    if slug.endswith(".git"):
        slug = slug[:-4]
    if "/" not in slug:
        return None
    owner, repo = slug.split("/", 1)
    if not owner or not repo:
        return None
    return owner, repo


def build_raw_url(owner: str, repo: str, path: str, branch: str = DEFAULT_BRANCH) -> str:
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path.lstrip('/')}"


def fetch_bytes(url: str) -> Optional[bytes]:
    dbg(f"GET {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "GitHubSync-Updater"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.read()
    except Exception as exc:
        print(f"[UPDATE] Failed to fetch: {url} ({exc})")
        return None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def version_tuple(ver: str) -> tuple:
    parts = []
    for part in str(ver).split("."):
        try:
            parts.append(int(part))
        except Exception:
            parts.append(0)
    return tuple(parts)


def has_newer(remote_ver: str, local_ver: str) -> bool:
    return version_tuple(remote_ver) > version_tuple(local_ver)


def prompt_update(remote_ver: str, local_ver: str) -> bool:
    """Ask the user whether to apply an available update.

    - Interactive console: prompt [Y/n] (default Yes)
    - Non-interactive: default Yes (so scheduled/hidden runs don't hang)
    """
    try:
        if not getattr(sys.stdin, "isatty", lambda: False)():
            dbg("stdin is not a TTY; auto-accepting update.")
            return True
    except Exception:
        return True

    msg = f"[UPDATE] New version available: {local_ver} -> {remote_ver}. Update now? [Y/n]: "
    try:
        ans = input(msg).strip().lower()
    except Exception:
        return True
    if ans in ("", "y", "yes"):
        return True
    if ans in ("n", "no"):
        return False
    # Any other input: be conservative and do not update.
    return False


def parse_manifest(manifest_text: str) -> Optional[Dict[str, object]]:
    try:
        obj = json.loads(manifest_text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def get_release_info(owner: str, repo: str) -> Optional[Dict[str, object]]:
    """Fetch fixed release manifest from GitHub stable branch.

    This implementation intentionally does NOT support any legacy/transition fallback.
    If the manifest is missing or invalid, update is skipped.
    """
    manifest_url = build_raw_url(owner, repo, RELEASE_MANIFEST_PATH, DEFAULT_BRANCH)
    raw = fetch_bytes(manifest_url)
    if not raw:
        return None
    txt = raw.decode("utf-8", errors="replace")
    mf = parse_manifest(txt)
    if mf and mf.get("version") and mf.get("exe_path"):
        return mf
    return None


def replace_and_restart_windows(downloaded: Path, target: Path) -> None:
    """
    Windows-safe self-replace:
      - wait for current PID to exit
      - copy new exe to DST.new
      - rename DST -> DST.old (best effort)
      - rename DST.new -> DST
      - restart DST
    """
    log_path = Path(tempfile.gettempdir()) / "githubsync_update.log"
    bat_path = Path(tempfile.gettempdir()) / "githubsync_update.bat"
    pid = os.getpid()

    dst_new = target.with_suffix(target.suffix + ".new")
    dst_old = target.with_suffix(target.suffix + ".old")

    # NOTE: Restart reliability
    # We intentionally *run the updated exe inline* ("%DST%") instead of relying
    # on `start` / PowerShell Start-Process, which can fail silently depending on
    # how the original exe was launched (PowerShell, Explorer, policies, etc.).
    # Running inline guarantees the updated process actually starts.
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "setlocal EnableDelayedExpansion",
        f'set "SRC={downloaded}"',
        f'set "DST={target}"',
        f'set "DSTNEW={dst_new}"',
        f'set "DSTOLD={dst_old}"',
        f'set "LOG={log_path}"',
        f'set "PID={pid}"',
        "del \"%LOG%\" >nul 2>&1",
        "call :LOG Updater started",
        "for %%I in (\"%DST%\") do set \"DSTDIR=%%~dpI\"",
        ":WAITLOOP",
        'tasklist /FI "PID eq %PID%" | find "%PID%" >nul',
        "if %errorlevel%==0 (",
        "  timeout /t 1 /nobreak >nul",
        "  goto WAITLOOP",
        ")",
        "call :LOG PID exited, starting replace",
        "if not exist \"%SRC%\" (call :LOG Source missing & goto END)",
        "copy /Y \"%SRC%\" \"%DSTNEW%\" >>\"%LOG%\" 2>&1",
        "if %errorlevel% neq 0 (call :LOG Copy to DSTNEW failed & goto END)",
        "if exist \"%DSTOLD%\" del /F /Q \"%DSTOLD%\" >nul 2>&1",
        "if exist \"%DST%\" (move /Y \"%DST%\" \"%DSTOLD%\" >>\"%LOG%\" 2>&1)",
        "move /Y \"%DSTNEW%\" \"%DST%\" >>\"%LOG%\" 2>&1",
        "if %errorlevel% neq 0 (call :LOG Move DSTNEW->DST failed & goto END)",
        "call :LOG Restarting",
        "call :LOG Launching updated exe (inline)",
        "pushd \"%DSTDIR%\"",
        "\"%DST%\"",
        'set "RUNERR=!errorlevel!"',
        "popd",
        "call :LOG Updated exe exited with code !RUNERR!",
        "del /F /Q \"%SRC%\" >nul 2>&1",
        "call :LOG Updater finished",
        ":END",
        "exit /b 0",
        ":LOG",
        'echo [%date% %time%] %*>>"%LOG%"',
        "exit /b 0",
    ]
    # Use UTF-8 with BOM so Windows cmd handles non-ASCII paths reliably.
    bat_path.write_text("\r\n".join(lines), encoding="utf-8-sig")
    print("[UPDATE] Applying update and restarting...")
    print(f"[UPDATE] Updater batch: {bat_path}")
    print(f"[UPDATE] Updater log:  {log_path}")
    try:
        # Attach updater to the current console so the restarted app is visible
        # when the user launched the exe from a terminal.
        subprocess.Popen(["cmd", "/c", str(bat_path)])
    except Exception as exc:
        print(f"[UPDATE] Failed to launch updater batch: {exc}")
        return
    os._exit(0)


def replace_and_restart(downloaded: Path, target: Path) -> None:
    if os.name == "nt":
        replace_and_restart_windows(downloaded, target)
    # Non-Windows: attempt atomic replace (best effort)
    try:
        target_tmp = target.with_suffix(target.suffix + ".new")
        target_tmp.write_bytes(downloaded.read_bytes())
        os.replace(str(target_tmp), str(target))
        subprocess.Popen([str(target)])
        os._exit(0)
    except Exception as exc:
        print(f"[UPDATE] Replace failed: {exc}")


def try_self_update(config: Dict[str, object], local_ver: str) -> bool:
    url = remote_url(config)
    if not url:
        print("[UPDATE] Remote repository is not embedded in this exe; skipping update check.")
        dbg("Remote URL not set in embedded config.")
        return False

    parsed = parse_remote(url)
    if not parsed:
        print("[UPDATE] Could not parse remote URL; skipping update check.")
        return False
    owner, repo = parsed

    info = get_release_info(owner, repo)
    if not info:
        print("[UPDATE] Release manifest not found or invalid; skipping update.")
        dbg("No release info found; skipping update.")
        return False

    remote_ver = str(info.get("version") or "0.0.0")
    exe_path = str(info.get("exe_path") or "")
    sha256_expected = str(info.get("sha256") or "").strip()

    dbg(f"Local version={local_ver}, Remote version={remote_ver}")
    if not has_newer(remote_ver, local_ver):
        return False

    # Ask the user before applying update (interactive console).
    if not prompt_update(remote_ver=remote_ver, local_ver=local_ver):
        print("[UPDATE] Skipped by user.")
        return False

    exe_url = build_raw_url(owner, repo, exe_path, DEFAULT_BRANCH)
    data = fetch_bytes(exe_url)
    if not data:
        print("[UPDATE] Download failed.")
        return False

    if sha256_expected:
        got = sha256_bytes(data)
        if got.lower() != sha256_expected.lower():
            print("[UPDATE] Integrity check failed (sha256 mismatch).")
            dbg(f"Expected={sha256_expected} Got={got}")
            return False

    with tempfile.NamedTemporaryFile(delete=False, suffix=".exe") as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)

    target = Path(sys.executable).resolve()
    replace_and_restart(tmp_path, target)
    return True


def run_main(local_ver: str) -> None:
    print(f"[BOOT] GitHubSync version {local_ver}")
    candidates = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "main.py")
        candidates.append(exe_dir / "main.py")
    candidates.append(Path(__file__).resolve().parent / "main.py")

    for path in candidates:
        if path.exists():
            runpy.run_path(str(path), run_name="__main__")
            return
    print("[BOOT] main.py not found; exiting.")


def main() -> None:
    _ = argparse.ArgumentParser(add_help=False).parse_args([])
    config = load_embedded_config()
    local_ver = app_version(config)
    if not config:
        run_main(local_ver)
        return
    updated = try_self_update(config, local_ver)
    if not updated:
        run_main(local_ver)


if __name__ == "__main__":
    main()
