#!/usr/bin/env python3
import glob
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone

VERSION = "0.1"
CONVERSATIONS_DIR = ".claude-history"
HOOK_GUARD_ENV = "GIT_CLAUDE_HISTORY_HOOK"
RESET_FILE = ".claude-history/.reset"
LISTEN_FILE = ".claude-history/.listen"
SILENT_FILE = ".claude-history/.silent"
INCLUDE_FILE = ".claude-history/.include"
EXCLUDE_FILE = ".claude-history/.exclude"


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _resolve_project_path(project_path: str = None) -> str:
    """Return project_path if given, else the git root of cwd, else cwd."""
    if project_path is not None:
        return project_path
    try:
        return subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        return os.getcwd()


def _read_timestamps(filepath: str) -> list[float]:
    """Return all newline-separated float timestamps from a file, or [] if absent."""
    try:
        with open(filepath) as f:
            return [float(l.strip()) for l in f if l.strip()]
    except (OSError, ValueError):
        return []


def _read_timestamp_set(filepath: str) -> set:
    """Return all timestamps from a file as a set, or empty set if absent."""
    return set(_read_timestamps(filepath))


def _remove_timestamp_from_file(filepath: str, ts: float) -> None:
    """Rewrite filepath without any line matching ts. No-op if file absent."""
    try:
        with open(filepath) as f:
            lines = f.readlines()
    except OSError:
        return
    kept = [l for l in lines if l.strip() and float(l.strip()) != ts]
    with open(filepath, "w") as f:
        f.writelines(kept)


def _append_timestamp(filepath: str) -> float:
    """Append the current UTC timestamp to filepath (creating it if needed). Returns the ts."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    ts = datetime.now(timezone.utc).timestamp()
    with open(filepath, "a") as f:
        f.write(str(ts) + "\n")
    return ts


def _get_cutoff(project_path: str) -> float:
    """Return max(HEAD commit timestamp, reset timestamp) for the given project."""
    return max(
        get_commit_timestamp(project_path, "HEAD"),
        get_reset_timestamp(project_path),
    )


# ---------------------------------------------------------------------------
# Public API — timestamps / markers
# ---------------------------------------------------------------------------

def find_project_jsonl_files(project_path: str = None) -> list[str]:
    """Find all Claude Code JSONL conversation files for the current project.

    Claude Code stores conversation files at:
      ~/.claude/projects/<encoded-path>/*.jsonl
    where the project path has every '/' replaced with '-'.

    Args:
        project_path: Absolute path to the project root. Defaults to the
                      git repository root of the current directory, falling
                      back to the current working directory.

    Returns:
        Sorted list of absolute paths to .jsonl files for this project.
    """
    project_path = _resolve_project_path(project_path)
    # Encode path the same way Claude Code does: replace every '/' with '-'
    # (the leading '/' becomes a leading '-')
    encoded = project_path.replace("/", "-")
    claude_project_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects", encoded)
    return sorted(glob.glob(os.path.join(claude_project_dir, "*.jsonl")))


def get_commit_timestamp(project_path: str = None, revision: str = "HEAD") -> float:
    """Return the Unix timestamp of a git revision, or 0.0 if it does not exist."""
    try:
        result = subprocess.run(
            ["git", "-C", project_path or os.getcwd(), "log", "-1", "--format=%ct", revision],
            capture_output=True, text=True, check=True,
        )
        value = result.stdout.strip()
        return float(value) if value else 0.0
    except subprocess.CalledProcessError:
        return 0.0


def get_reset_timestamp(project_path: str = None) -> float:
    """Return the Unix timestamp stored in the reset marker file, or 0.0 if absent."""
    path = os.path.join(_resolve_project_path(project_path), RESET_FILE)
    try:
        with open(path) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return 0.0


def run_reset(project_path: str = None) -> None:
    """Write the current timestamp to the reset marker file.

    Future runs will ignore any conversation messages recorded before this moment.
    """
    project_path = _resolve_project_path(project_path)
    path = os.path.join(project_path, RESET_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ts = datetime.now(timezone.utc).timestamp()
    with open(path, "w") as f:
        f.write(str(ts))
    human_ts = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[claude-history] Reset timestamp set to {human_ts}. Messages before this will be ignored.")


def get_listen_timestamp(project_path: str = None) -> float:
    """Return the Unix timestamp stored in the listen marker file, or 0.0 if absent."""
    tss = _read_timestamps(os.path.join(_resolve_project_path(project_path), LISTEN_FILE))
    return tss[-1] if tss else 0.0


def run_listen(project_path: str = None) -> None:
    """Write the current timestamp to the listen marker file.

    Until this is called, the hook will remain silent. After this call,
    the hook will capture conversation history on each commit.
    """
    project_path = _resolve_project_path(project_path)
    ts = _append_timestamp(os.path.join(project_path, LISTEN_FILE))
    human_ts = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[claude-history] Now listening from {human_ts}. Conversation history will be saved on commit.")


def get_silent_timestamp(project_path: str = None) -> float:
    """Return the Unix timestamp stored in the silent marker file, or 0.0 if absent."""
    tss = _read_timestamps(os.path.join(_resolve_project_path(project_path), SILENT_FILE))
    return tss[-1] if tss else 0.0


def run_silent(project_path: str = None) -> None:
    """Write the current timestamp to the silent marker file.

    After this is called, the hook will stop capturing conversation history.
    """
    project_path = _resolve_project_path(project_path)
    ts = _append_timestamp(os.path.join(project_path, SILENT_FILE))
    human_ts = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[claude-history] Silent from {human_ts}. No further conversation history will be saved.")


def get_include_timestamps(project_path: str = None) -> set:
    """Return the set of Unix timestamps explicitly force-included via 'chistory include'."""
    return _read_timestamp_set(os.path.join(_resolve_project_path(project_path), INCLUDE_FILE))


def get_exclude_timestamps(project_path: str = None) -> set:
    """Return the set of Unix timestamps explicitly force-excluded via 'chistory exclude'."""
    return _read_timestamp_set(os.path.join(_resolve_project_path(project_path), EXCLUDE_FILE))


def _toggle_entry_marker(n: int, project_path: str, add_file: str, remove_file: str, verb: str) -> None:
    """Move the entry at position N from remove_file to add_file.

    Used by run_include (add=INCLUDE_FILE, remove=EXCLUDE_FILE) and
    run_exclude (add=EXCLUDE_FILE, remove=INCLUDE_FILE).
    """
    entries = _collect_entries(project_path, _get_cutoff(project_path))

    if n < 1 or n > len(entries):
        print(
            f"[claude-history] Error: id {n} is out of range (1–{len(entries)}).",
            file=sys.stderr,
        )
        sys.exit(1)

    ts, role, _text = entries[n - 1]
    _remove_timestamp_from_file(os.path.join(project_path, remove_file), ts)
    add_path = os.path.join(project_path, add_file)
    os.makedirs(os.path.dirname(add_path), exist_ok=True)
    if ts not in _read_timestamp_set(add_path):
        with open(add_path, "a") as f:
            f.write(str(ts) + "\n")
    print(f"[claude-history] Message {n} ({role}) force-{verb}.")


def _bulk_toggle_entry_markers(
    ns: list[int], project_path: str, add_file: str, remove_file: str, verb: str
) -> None:
    """Toggle markers for multiple ids in a single pass — O(N) instead of O(N²).

    Collects entries once, validates all ids, then rewrites each marker file once.
    """
    entries = _collect_entries(project_path, _get_cutoff(project_path))

    invalid = [n for n in ns if n < 1 or n > len(entries)]
    if invalid:
        print(
            f"[claude-history] Error: id(s) {invalid} out of range (1–{len(entries)}).",
            file=sys.stderr,
        )
        sys.exit(1)

    toggle_ts = {entries[n - 1][0] for n in ns}

    # Remove from remove_file in one pass
    remove_path = os.path.join(project_path, remove_file)
    try:
        with open(remove_path) as f:
            lines = f.readlines()
        kept = [l for l in lines if not (l.strip() and float(l.strip()) in toggle_ts)]
        with open(remove_path, "w") as f:
            f.writelines(kept)
    except OSError:
        pass

    # Append only new timestamps to add_file in one pass
    add_path = os.path.join(project_path, add_file)
    os.makedirs(os.path.dirname(add_path), exist_ok=True)
    existing = _read_timestamp_set(add_path)
    new_ts = toggle_ts - existing
    if new_ts:
        with open(add_path, "a") as f:
            for ts in new_ts:
                f.write(str(ts) + "\n")

    for n in ns:
        ts, role, _ = entries[n - 1]
        print(f"[claude-history] Message {n} ({role}) force-{verb}.")


def run_include(n: int, project_path: str = None) -> None:
    """Force-include the message at position N from 'chistory preview --full'.

    Removes the message's timestamp from the exclude file (if present) and
    appends it to the include file so it bypasses window filtering.
    """
    _toggle_entry_marker(n, _resolve_project_path(project_path), INCLUDE_FILE, EXCLUDE_FILE, "included")


def run_exclude(n: int, project_path: str = None) -> None:
    """Force-exclude the message at position N from 'chistory preview --full'.

    Removes the message's timestamp from the include file (if present) and
    appends it to the exclude file so it is suppressed regardless of windows.
    """
    _toggle_entry_marker(n, _resolve_project_path(project_path), EXCLUDE_FILE, INCLUDE_FILE, "excluded")


# ---------------------------------------------------------------------------
# Window logic
# ---------------------------------------------------------------------------

def _build_windows(listen_tss: list[float], silent_tss: list[float]) -> list[tuple]:
    """Build (start, end) windows by replaying listen/silent events in chronological order.

    A 'listen' while not currently listening opens a new window.
    A 'silent' while listening closes the current window.
    Extra silents before any listen, or consecutive listens, are ignored.
    An unclosed window at the end has end=None (open-ended).
    """
    events = sorted(
        [(ts, "listen") for ts in listen_tss] +
        [(ts, "silent") for ts in silent_tss]
    )
    windows = []
    window_start = None
    for ts, kind in events:
        if kind == "listen" and window_start is None:
            window_start = ts
        elif kind == "silent" and window_start is not None:
            windows.append((window_start, ts))
            window_start = None
    if window_start is not None:
        windows.append((window_start, None))
    return windows


def _in_window(ts: float, windows: list[tuple]) -> bool:
    """Return True if ts falls inside any of the given (start, end) windows."""
    return any(start < ts and (end is None or ts <= end) for start, end in windows)


# ---------------------------------------------------------------------------
# Entry collection and filtering
# ---------------------------------------------------------------------------

def _collect_entries(project_path: str, cutoff: float) -> list[tuple]:
    """Return (timestamp, role, text) for every message since cutoff, unfiltered.

    Assistant streaming chunks are reassembled; only completed messages are returned.
    """
    jsonl_files = find_project_jsonl_files(project_path)

    user_entries = []   # (ts, uuid, text)
    asst_by_id = {}     # message_id -> {"ts", "uuid", "parts": [str], "done": bool}

    for path in jsonl_files:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                entry_type = entry.get("type")
                ts_str = entry.get("timestamp", "")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                except (ValueError, AttributeError):
                    continue

                if ts <= cutoff:
                    continue

                if entry_type == "user":
                    content = entry.get("message", {}).get("content", "")
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        parts = [b.get("text", "") for b in content if b.get("type") == "text"]
                        text = "\n".join(parts)
                    else:
                        continue
                    text = re.sub(r"<[^>]*>.*?</[^>]*>", "", text, flags=re.DOTALL)
                    text = re.sub(r"<[^>]+>", "", text)
                    if text.strip():
                        user_entries.append((ts, entry["uuid"], text.strip()))

                elif entry_type == "assistant":
                    msg = entry.get("message", {})
                    msg_id = msg.get("id", entry["uuid"])
                    chunk_parts = [
                        block.get("text", "")
                        for block in msg.get("content", [])
                        if block.get("type") == "text"
                    ]
                    chunk_text = "\n".join(chunk_parts)
                    if msg_id not in asst_by_id:
                        asst_by_id[msg_id] = {"ts": ts, "uuid": entry["uuid"], "parts": [], "done": False}
                    if chunk_text:
                        asst_by_id[msg_id]["parts"].append(chunk_text)
                    if msg.get("stop_reason"):
                        asst_by_id[msg_id]["done"] = True

    asst_entries = []
    for acc in asst_by_id.values():
        if not acc["done"]:
            continue
        text = "\n".join(acc["parts"]).strip()
        if text:
            asst_entries.append((acc["ts"], acc["uuid"], text))

    all_entries = (
        [(ts, uuid, "User", text) for ts, uuid, text in user_entries] +
        [(ts, uuid, "Assistant", text) for ts, uuid, text in asst_entries]
    )
    all_entries.sort(key=lambda x: x[0])
    return [(ts, role, text) for ts, _uuid, role, text in all_entries]


def _is_included(ts: float, windows: list[tuple], include_timestamps: set, exclude_timestamps: set) -> bool:
    """Return True if a message timestamp should appear in output."""
    return ts in include_timestamps or (ts not in exclude_timestamps and _in_window(ts, windows))


def messages_since_last_commit(project_path: str = None, cutoff: float = None, windows: list = None) -> list[str]:
    """Return formatted strings for every user message and assistant response
    recorded in the project's JSONL files since the last git commit.

    Args:
        project_path: Absolute path to the project root. Defaults to the git
                      repository root of the current directory, falling back
                      to cwd.
        cutoff: Unix timestamp to filter from. Defaults to the HEAD commit timestamp.
        windows: Optional list of (start, end) pairs from _build_windows. When
                 provided, only messages whose timestamp falls within at least one
                 window are included. end=None means the window is open-ended.

    Returns:
        List of strings, each prefixed with "User: " or "Assistant: ",
        ordered by timestamp.
    """
    project_path = _resolve_project_path(project_path)

    if cutoff is None:
        cutoff = get_commit_timestamp(project_path, "HEAD")
    cutoff = max(cutoff, get_reset_timestamp(project_path))

    entries = _collect_entries(project_path, cutoff)

    if windows is not None:
        include_timestamps = get_include_timestamps(project_path)
        exclude_timestamps = get_exclude_timestamps(project_path)
        entries = [
            (ts, role, text) for ts, role, text in entries
            if _is_included(ts, windows, include_timestamps, exclude_timestamps)
        ]

    return [f"{role}: {text}" for _ts, role, text in entries]


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def write_conversation_md(output_path: str, project_path: str = None, cutoff: float = None, messages: list[str] = None) -> str:
    """Write messages since a given cutoff to a Markdown file.

    Args:
        output_path: Destination file path for the markdown output.
        project_path: Project root passed through to messages_since_last_commit.
        cutoff: Unix timestamp passed through to messages_since_last_commit.
        messages: Pre-computed message list; fetched if not provided.

    Returns:
        The path the file was written to.
    """
    if messages is None:
        messages = messages_since_last_commit(project_path, cutoff=cutoff)

    lines = ["# Conversation since last commit\n"]
    for entry in messages:
        role, _, text = entry.partition(": ")
        lines.append(f"### {role}\n")
        lines.append(f"{text}\n")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        f.write("\n".join(lines))

    return output_path


def _format_as_md(messages: list[str]) -> str:
    """Format a list of 'Role: text' strings as a Markdown conversation."""
    lines = ["# Conversation since last commit\n"]
    for entry in messages:
        role, _, text = entry.partition(": ")
        lines.append(f"### {role}\n")
        lines.append(f"{text}\n")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def run_preview(project_path: str = None) -> None:
    """Print to stdout the conversation that would be saved on the next commit.

    Same logic as run_hook but uses HEAD as the lower cutoff (since no commit
    has happened yet) and writes to stdout instead of a file.
    """
    project_path = _resolve_project_path(project_path)

    listen_tss = _read_timestamps(os.path.join(project_path, LISTEN_FILE))
    if not listen_tss:
        print("[claude-history] Not listening. Run 'chistory listen' to start.")
        return

    silent_tss = _read_timestamps(os.path.join(project_path, SILENT_FILE))
    windows = _build_windows(listen_tss, silent_tss)

    messages = messages_since_last_commit(project_path, cutoff=_get_cutoff(project_path), windows=windows)
    if not messages:
        print("[claude-history] No new conversation messages to preview.")
        return

    print(_format_as_md(messages))


def run_preview_full(project_path: str = None) -> None:
    """Print all messages since the last commit with CH[N-o/x] inclusion markers.

    Every message is shown regardless of listen/silent windows. Each gets a
    sequential id prefixed as CH[N-o] (inside a window) or CH[N-x] (outside).
    """
    project_path = _resolve_project_path(project_path)

    listen_tss = _read_timestamps(os.path.join(project_path, LISTEN_FILE))
    silent_tss = _read_timestamps(os.path.join(project_path, SILENT_FILE))
    windows = _build_windows(listen_tss, silent_tss)

    entries = _collect_entries(project_path, _get_cutoff(project_path))
    if not entries:
        print("[claude-history] No conversation messages since last commit.")
        return

    include_timestamps = get_include_timestamps(project_path)
    exclude_timestamps = get_exclude_timestamps(project_path)
    lines = []
    for n, (ts, role, text) in enumerate(entries, start=1):
        marker = "o" if _is_included(ts, windows, include_timestamps, exclude_timestamps) else "x"
        lines.append(f"CH[{n}-{marker}]")
        lines.append(f"### {role}\n")
        lines.append(f"{text}\n")

    print("\n".join(lines))


def run_help() -> None:
    """Print usage information for all chistory commands."""
    print(f"""\
chistory {VERSION} — save Claude Code conversation history alongside git commits

USAGE
  chistory <command> [args]

SETUP
  chistory --install        Install the post-commit hook into the current git project.
                            Run this once per project you want to track.

LISTENING
  chistory listen           Start capturing conversation history on future commits.
  chistory silent           Stop capturing conversation history.

PREVIEW & REVIEW
  chistory preview          Show the conversation that would be saved on the next commit.
  chistory preview --full   Show all messages since the last commit with inclusion markers.
                            Each message is labeled CH[N-o] (included) or CH[N-x] (excluded).

FINE-GRAINED CONTROL
  chistory include <id>     Force-include message <id> regardless of listen/silent windows.
  chistory include --all    Force-include all messages.
  chistory exclude <id>     Force-exclude message <id> regardless of listen/silent windows.
  chistory exclude --all    Force-exclude all messages.
  chistory include <x>..<y> Force-include messages in the inclusive range x..y.
  chistory exclude <x>..<y> Force-exclude messages in the inclusive range x..y.

OTHER
  chistory reset            Ignore all conversation history before the current moment.
  chistory version          Print the current version.
  chistory help             Print this help message.
  chistory                  Write conversation.md from messages since the last commit.\
""")


def run_hook() -> None:
    """Execute the post-commit hook logic.

    - Skips if the re-entry guard env var is set (prevents amend looping).
    - Determines the conversation window as (HEAD~1 timestamp, now].
    - Writes the conversation to .claude-history/<short-hash>.md.
    - Amends the commit message to append a link to that file.
    """
    if os.environ.get(HOOK_GUARD_ENV):
        return

    project_path = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    listen_tss = _read_timestamps(os.path.join(project_path, LISTEN_FILE))
    if not listen_tss:
        return

    silent_tss = _read_timestamps(os.path.join(project_path, SILENT_FILE))
    windows = _build_windows(listen_tss, silent_tss)

    commit_hash = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    short_hash = commit_hash[:8]

    # Cutoff is the commit before this one so we capture only what changed,
    # or the reset timestamp if more recent.
    cutoff = max(
        get_commit_timestamp(project_path, "HEAD~1"),
        get_reset_timestamp(project_path),
    )

    messages = messages_since_last_commit(project_path, cutoff=cutoff, windows=windows)
    if not messages:
        return

    conv_dir = os.path.join(project_path, CONVERSATIONS_DIR)
    output_path = os.path.join(conv_dir, f"{short_hash}.md")
    write_conversation_md(output_path, project_path=project_path, cutoff=cutoff, messages=messages)

    original_msg = subprocess.run(
        ["git", "log", "-1", "--format=%B"],
        capture_output=True, text=True, check=True,
    ).stdout.rstrip()

    relative_path = os.path.relpath(output_path, project_path)
    new_msg = f"{original_msg}\n\nConversation history: {relative_path}"

    env = {**os.environ, HOOK_GUARD_ENV: "1"}
    subprocess.run(["git", "add", output_path], check=True)
    subprocess.run(
        ["git", "commit", "--amend", "-m", new_msg],
        env=env, check=True,
    )

    print(f"[claude-history] Saved conversation to {relative_path}")


def install_hook(project_path: str = None) -> None:
    """Install main.py as the post-commit git hook for the project.

    Writes .git/hooks/post-commit as a shell script that invokes this file
    with python3. Marks it executable.

    Args:
        project_path: Project root. Defaults to the git root of cwd.
    """
    if project_path is None:
        project_path = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    script_path = os.path.abspath(__file__)
    hook_path = os.path.join(project_path, ".git", "hooks", "post-commit")

    hook_content = f"""#!/bin/sh
python3 "{script_path}" --hook
"""
    with open(hook_path, "w") as f:
        f.write(hook_content)

    current = stat.S_IMODE(os.stat(hook_path).st_mode)
    os.chmod(hook_path, current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    print(f"Installed post-commit hook at {hook_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _all_entry_ids(project_path: str = None) -> list[int]:
    """Return [1, 2, ..., N] for every entry since the last commit."""
    project_path = _resolve_project_path(project_path)
    entries = _collect_entries(project_path, _get_cutoff(project_path))
    return list(range(1, len(entries) + 1))


def _parse_id_arg(arg: str) -> list[int]:
    """Parse a single id or an inclusive range ('x..y') into a list of ints.

    Exits with an error message if the argument is not a valid int or range.
    """
    if ".." in arg:
        parts = arg.split("..", 1)
        try:
            start, end = int(parts[0]), int(parts[1])
        except ValueError:
            print(f"[claude-history] Error: range must be integers (got {arg!r}).", file=sys.stderr)
            sys.exit(1)
        return list(range(start, end + 1))
    try:
        return [int(arg)]
    except ValueError:
        print(f"[claude-history] Error: id must be an integer (got {arg!r}).", file=sys.stderr)
        sys.exit(1)


def cli() -> None:
    """Entry point for the `chistory` command."""
    if "--hook" in sys.argv:
        run_hook()
    elif "--install" in sys.argv:
        install_hook()
    elif (len(sys.argv) > 2 and sys.argv[1] == "preview" and sys.argv[2] == "--full") or "--preview-full" in sys.argv:
        run_preview_full()
    elif "--preview" in sys.argv or (len(sys.argv) > 1 and sys.argv[1] == "preview"):
        run_preview()
    elif "--listen" in sys.argv or (len(sys.argv) > 1 and sys.argv[1] == "listen"):
        run_listen()
    elif "--silent" in sys.argv or (len(sys.argv) > 1 and sys.argv[1] == "silent"):
        run_silent()
    elif "--reset" in sys.argv or (len(sys.argv) > 1 and sys.argv[1] == "reset"):
        run_reset()
    elif len(sys.argv) > 2 and sys.argv[1] == "include":
        project_path = _resolve_project_path()
        ns = _all_entry_ids(project_path) if sys.argv[2] == "--all" else _parse_id_arg(sys.argv[2])
        _bulk_toggle_entry_markers(ns, project_path, INCLUDE_FILE, EXCLUDE_FILE, "included")
    elif len(sys.argv) > 1 and sys.argv[1] in ("version", "--version"):
        print(f"chistory {VERSION}")
    elif len(sys.argv) > 1 and sys.argv[1] in ("help", "--help", "-h"):
        run_help()
    elif len(sys.argv) > 2 and sys.argv[1] == "exclude":
        project_path = _resolve_project_path()
        ns = _all_entry_ids(project_path) if sys.argv[2] == "--all" else _parse_id_arg(sys.argv[2])
        _bulk_toggle_entry_markers(ns, project_path, EXCLUDE_FILE, INCLUDE_FILE, "excluded")
    else:
        # Manual run: write conversation.md from last commit to cwd
        output = write_conversation_md("conversation.md")
        print(f"Written to {output}")


if __name__ == "__main__":
    cli()
