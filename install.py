#!/usr/bin/env python3
"""
One-command installer for the OpenSooq MCP server.

Run it with any Python:  `python install.py`   (it only orchestrates — no deps).

What it does:
  1. Finds `uv` (the only prerequisite); prints how to get it if missing.
  2. Makes sure the pinned Python (from .python-version) + the server's deps are
     downloaded and cached, so the first client launch is instant — and verifies
     the server actually imports.
  3. Builds the MCP config block (absolute `uv` path + this folder's absolute
     path, so it works regardless of PATH or working directory).
  4. Auto-writes that block into every MCP client it detects on this machine
     (Claude Desktop, Cursor, Antigravity/Gemini, Windsurf) — backing up each
     file first — and prints the block for anything it didn't touch.

Flags:
  --print-only   Don't modify any client config; just print the block + targets.
  --name NAME    Server name in the config (default: opensooq).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY_PIN = (HERE / ".python-version").read_text(encoding="utf-8").strip() if (HERE / ".python-version").exists() else "3.12"


# --------------------------------------------------------------------------- #
# locate uv
# --------------------------------------------------------------------------- #

def find_uv() -> str | None:
    onpath = shutil.which("uv")
    if onpath:
        return onpath
    home = Path.home()
    candidates = [
        home / ".local" / "bin" / "uv",
        home / ".local" / "bin" / "uv.exe",
        home / ".cargo" / "bin" / "uv",
        home / ".cargo" / "bin" / "uv.exe",
        Path(os.environ.get("APPDATA", "")) / "Python" / "Python39" / "Scripts" / "uv.exe",
    ]
    # any user-site Scripts dir on Windows
    appdata_roaming = Path(os.environ.get("APPDATA", ""))
    if appdata_roaming.exists():
        for d in (appdata_roaming / "Python").glob("Python*/Scripts/uv.exe"):
            candidates.append(d)
    for c in candidates:
        if c and Path(c).exists():
            return str(c)
    return None


UV_INSTALL_HINT = (
    "uv is not installed. Install it (one command), then re-run this script:\n"
    "  macOS/Linux : curl -LsSf https://astral.sh/uv/install.sh | sh\n"
    "  Windows     : powershell -ExecutionPolicy ByPass -c \"irm https://astral.sh/uv/install.ps1 | iex\"\n"
    "Then restart your terminal so `uv` is on PATH."
)


# --------------------------------------------------------------------------- #
# provision + verify
# --------------------------------------------------------------------------- #

def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kw)


def ensure_runtime(uv: str) -> bool:
    print(f"• Ensuring Python {PY_PIN} is available to uv …")
    r = run([uv, "python", "install", PY_PIN], capture_output=True, text=True)
    if r.returncode != 0 and "No such host" in (r.stderr or ""):
        print("  ! Could not reach GitHub to download Python. If it's already cached "
              "this is fine; otherwise connect to the internet once and re-run.")
    print("• Caching dependencies and verifying the server imports …")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    r = run([uv, "run", "--directory", str(HERE), "python", "-c",
             "import server; print('  ✓ server loaded:', len(__import__('asyncio').run(server.mcp.list_tools())), 'tools')"],
            text=True, env=env)
    return r.returncode == 0


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

def server_entry(uv: str, name: str) -> dict:
    return {
        "command": uv,
        "args": ["run", "--directory", str(HERE), "server.py"],
        "env": {"PYTHONIOENCODING": "utf-8"},
    }


def client_targets() -> dict[str, tuple[Path, Path]]:
    """name -> (marker dir that means the app is installed, config file path)."""
    home = Path.home()
    sysname = platform.system()
    if sysname == "Windows":
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        claude = appdata / "Claude" / "claude_desktop_config.json"
        claude_marker = appdata / "Claude"
    elif sysname == "Darwin":
        claude = home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        claude_marker = claude.parent
    else:
        claude = home / ".config" / "Claude" / "claude_desktop_config.json"
        claude_marker = claude.parent
    return {
        "Claude Desktop": (claude_marker, claude),
        "Cursor": (home / ".cursor", home / ".cursor" / "mcp.json"),
        "Antigravity (Gemini)": (home / ".gemini", home / ".gemini" / "config" / "mcp_config.json"),
        "Windsurf": (home / ".codeium", home / ".codeium" / "windsurf" / "mcp_config.json"),
    }


def merge_into(path: Path, name: str, entry: dict) -> str:
    """Merge our server entry into an MCP config file. Returns a status string."""
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except Exception:
            backup = path.with_suffix(path.suffix + ".broken.bak")
            shutil.copy2(path, backup)
            return f"existing file was not valid JSON; backed up to {backup.name} and skipped"
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    servers = data.setdefault("mcpServers", {})
    action = "updated" if name in servers else "added"
    servers[name] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return action


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="Install the OpenSooq MCP server into your MCP clients.")
    ap.add_argument("--print-only", action="store_true", help="Only print the config; don't modify client files.")
    ap.add_argument("--name", default="opensooq", help="Server name in the config (default: opensooq).")
    args = ap.parse_args()

    print("OpenSooq MCP — installer\n" + "=" * 28)
    uv = find_uv()
    if not uv:
        print("✗ " + UV_INSTALL_HINT)
        return 1
    print(f"• Found uv: {uv}")

    ok = ensure_runtime(uv)
    if not ok:
        print("✗ The server failed to start under uv. Fix the error above and re-run.")
        return 1

    entry = server_entry(uv, args.name)
    block = {"mcpServers": {args.name: entry}}
    pretty = json.dumps(block, indent=2, ensure_ascii=False)

    print("\nConfig block (this is what gets installed):\n")
    print(pretty)

    if args.print_only:
        print("\n(--print-only) Paste the block above into your client's MCP config.")
        return 0

    print("\n• Installing into detected MCP clients …")
    any_written = False
    for cname, (marker, cfg) in client_targets().items():
        if marker.exists():
            status = merge_into(cfg, args.name, entry)
            print(f"  ✓ {cname}: {status}  ->  {cfg}")
            any_written = True
        else:
            print(f"  – {cname}: not detected (skipped)")

    if any_written:
        print("\nDone. Fully restart the client(s) above, then ask: "
              "\"search OpenSooq for iphone 15 pro\".")
    else:
        print("\nNo known MCP clients detected. Paste the block above into your client's config manually.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
