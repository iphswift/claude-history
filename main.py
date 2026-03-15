#!/usr/bin/env python3
import glob
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone

CONVERSATIONS_DIR = ".claude-history"
HOOK_GUARD_ENV = "GIT_CLAUDE_HISTORY_HOOK"
RESET_FILE = ".claude-history/.reset"


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
    if project_path is None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, check=True,
            )
            project_path = result.stdout.strip()
        except subprocess.CalledProcessError:
            project_path = os.getcwd()

    # Encode path the same way Claude Code does: replace every '/' with '-'
    # (the leading '/' becomes a leading '-')
    encoded = project_path.replace("/", "-")

    claude_project_dir = os.path.join(
        os.path.expanduser("~"), ".claude", "projects", encoded
    )
    pattern = os.path.join(claude_project_dir, "*.jsonl")
    return sorted(glob.glob(pattern))


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
    if project_path is None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, check=True,
            )
            project_path = result.stdout.strip()
        except subprocess.CalledProcessError:
            project_path = os.getcwd()

    reset_path = os.path.join(project_path, RESET_FILE)
    try:
        with open(reset_path) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return 0.0


def run_reset(project_path: str = None) -> None:
    """Write the current timestamp to the reset marker file.

    Future runs will ignore any conversation messages recorded before this moment.
    """
    if project_path is None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, check=True,
            )
            project_path = result.stdout.strip()
        except subprocess.CalledProcessError:
            project_path = os.getcwd()

    reset_path = os.path.join(project_path, RESET_FILE)
    os.makedirs(os.path.dirname(reset_path), exist_ok=True)
    ts = datetime.now(timezone.utc).timestamp()
    with open(reset_path, "w") as f:
        f.write(str(ts))

    human_ts = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[claude-history] Reset timestamp set to {human_ts}. Messages before this will be ignored.")


def messages_since_last_commit(project_path: str = None, cutoff: float = None) -> list[str]:
    """Return formatted strings for every user message and assistant response
    recorded in the project's JSONL files since the last git commit.

    Assistant streaming produces multiple JSONL entries per logical message;
    only the final entry (where stop_reason is set) is used.

    Args:
        project_path: Absolute path to the project root. Defaults to the git
                      repository root of the current directory, falling back
                      to cwd.
        cutoff: Unix timestamp to filter from. Defaults to the HEAD commit timestamp.

    Returns:
        List of strings, each prefixed with "User: " or "Assistant: ",
        ordered by timestamp.
    """
    if project_path is None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, check=True,
            )
            project_path = result.stdout.strip()
        except subprocess.CalledProcessError:
            project_path = os.getcwd()

    if cutoff is None:
        cutoff = get_commit_timestamp(project_path, "HEAD")

    cutoff = max(cutoff, get_reset_timestamp(project_path))

    jsonl_files = find_project_jsonl_files(project_path)

    # Collect qualifying entries; for assistant messages accumulate text across
    # all streaming chunks (each entry may only contain a partial delta), and
    # mark complete once stop_reason appears.
    user_entries = []   # (timestamp_float, uuid, text)
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

    # Extract text from completed assistant messages
    asst_entries = []
    for acc in asst_by_id.values():
        if not acc["done"]:
            continue
        text = "\n".join(acc["parts"]).strip()
        if text:
            asst_entries.append((acc["ts"], acc["uuid"], text))

    # Merge and sort by timestamp
    all_entries = (
        [(ts, uuid, "User", text) for ts, uuid, text in user_entries] +
        [(ts, uuid, "Assistant", text) for ts, uuid, text in asst_entries]
    )
    all_entries.sort(key=lambda x: x[0])

    return [f"{role}: {text}" for _, _, role, text in all_entries]


def write_conversation_md(output_path: str, project_path: str = None, cutoff: float = None) -> str:
    """Write messages since a given cutoff to a Markdown file.

    Args:
        output_path: Destination file path for the markdown output.
        project_path: Project root passed through to messages_since_last_commit.
        cutoff: Unix timestamp passed through to messages_since_last_commit.

    Returns:
        The path the file was written to.
    """
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

    commit_hash = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    short_hash = commit_hash[:8]

    # Cutoff is the commit before this one so we capture only what changed,
    # or the reset timestamp if it's more recent.
    cutoff = max(
        get_commit_timestamp(project_path, "HEAD~1"),
        get_reset_timestamp(project_path),
    )

    conv_dir = os.path.join(project_path, CONVERSATIONS_DIR)
    output_path = os.path.join(conv_dir, f"{short_hash}.md")
    write_conversation_md(output_path, project_path=project_path, cutoff=cutoff)

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


def cli() -> None:
    """Entry point for the `chistory` command."""
    if "--hook" in sys.argv:
        run_hook()
    elif "--install" in sys.argv:
        install_hook()
    elif "--reset" in sys.argv or (len(sys.argv) > 1 and sys.argv[1] == "reset"):
        run_reset()
    else:
        # Manual run: write conversation.md from last commit to cwd
        output = write_conversation_md("conversation.md")
        print(f"Written to {output}")


if __name__ == "__main__":
    cli()
