#!/usr/bin/env python3
"""Tests for the claude-history post-commit hook."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

# Always execute relative to this directory, regardless of where the script is run from.
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(TEST_DIR)

PROJECT_ROOT = os.path.dirname(TEST_DIR)
MAIN_PY = os.path.join(PROJECT_ROOT, "main.py")


def git(args, cwd, env=None, check=True):
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
        env={**os.environ, **(env or {})},
    )


class IsolatedRepo:
    """An isolated git repository for a single test, independent of the main repo."""

    def setup(self):
        self.path = tempfile.mkdtemp(prefix="chistory_test_")
        git(["init"], cwd=self.path)
        git(["config", "user.email", "test@example.com"], cwd=self.path)
        git(["config", "user.name", "Test User"], cwd=self.path)

        hook_path = os.path.join(self.path, ".git", "hooks", "post-commit")
        with open(hook_path, "w") as f:
            f.write(f'#!/bin/sh\npython3 "{MAIN_PY}" --hook\n')
        os.chmod(hook_path, 0o755)

    def teardown(self):
        shutil.rmtree(self.path, ignore_errors=True)

    def commit(self, message="test commit", filename="file.txt", content="hello\n"):
        filepath = os.path.join(self.path, filename)
        with open(filepath, "w") as f:
            f.write(content)
        git(["add", filename], cwd=self.path)
        git(["commit", "-m", message], cwd=self.path)

    def commit_message(self):
        return git(["log", "-1", "--format=%B"], cwd=self.path).stdout.strip()

    def committed_files(self):
        output = git(["show", "--name-only", "--format="], cwd=self.path).stdout.strip()
        return [f for f in output.splitlines() if f]


class TestSilentCommand(unittest.TestCase):
    """Tests for the listen/silent window: only messages between the two calls should appear."""

    # Timeline: T1(before) < listen(09:00) < T3(in-window) < silent(11:00) < T5(after)
    LISTEN_TS = datetime(2026, 3, 15, 9, 0, 0, tzinfo=timezone.utc).timestamp()
    SILENT_TS = datetime(2026, 3, 15, 11, 0, 0, tzinfo=timezone.utc).timestamp()

    def setUp(self):
        self.repo = IsolatedRepo()
        self.repo.setup()

        encoded = self.repo.path.replace("/", "-")
        self.stub_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects", encoded)
        os.makedirs(self.stub_dir, exist_ok=True)
        self.stub_jsonl = os.path.join(self.stub_dir, "stub.jsonl")

        stub_lines = [
            # T1 = 08:00 — before listen
            '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"before listen message"},"uuid":"u1","timestamp":"2026-03-15T08:00:00.000Z"}',
            '{"parentUuid":"u1","isSidechain":false,"type":"assistant","message":{"id":"m1","role":"assistant","content":[{"type":"text","text":"ack before"}],"stop_reason":"end_turn"},"uuid":"u2","timestamp":"2026-03-15T08:00:01.000Z"}',
            # T3 = 10:00 — after listen, before silent
            '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"in window message"},"uuid":"u3","timestamp":"2026-03-15T10:00:00.000Z"}',
            '{"parentUuid":"u3","isSidechain":false,"type":"assistant","message":{"id":"m3","role":"assistant","content":[{"type":"text","text":"ack in window"}],"stop_reason":"end_turn"},"uuid":"u4","timestamp":"2026-03-15T10:00:01.000Z"}',
            # T5 = 12:00 — after silent
            '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"after silent message"},"uuid":"u5","timestamp":"2026-03-15T12:00:00.000Z"}',
            '{"parentUuid":"u5","isSidechain":false,"type":"assistant","message":{"id":"m5","role":"assistant","content":[{"type":"text","text":"ack after silent"}],"stop_reason":"end_turn"},"uuid":"u6","timestamp":"2026-03-15T12:00:01.000Z"}',
        ]
        with open(self.stub_jsonl, "w") as f:
            f.write("\n".join(stub_lines) + "\n")

        self._write_marker(".claude-history/.listen", self.LISTEN_TS)
        self._write_marker(".claude-history/.silent", self.SILENT_TS)

    def _write_marker(self, rel_path, ts):
        path = os.path.join(self.repo.path, rel_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(str(ts))

    def tearDown(self):
        if os.path.exists(self.stub_jsonl):
            os.remove(self.stub_jsonl)
        try:
            os.rmdir(self.stub_dir)
        except OSError:
            pass
        self.repo.teardown()

    def _conversation_content(self):
        """Return the text of the conversation .md committed by the hook, or ''."""
        for f in self.repo.committed_files():
            if f.startswith(".claude-history/") and f.endswith(".md"):
                with open(os.path.join(self.repo.path, f)) as fh:
                    return fh.read()
        return ""

    def test_before_listen_excluded(self):
        """Messages before 'chistory listen' must not appear in the conversation file."""
        self.repo.commit(message="add feature")
        content = self._conversation_content()
        self.assertNotIn("before listen message", content)

    def test_in_window_included(self):
        """Messages between 'chistory listen' and 'chistory silent' must appear."""
        self.repo.commit(message="add feature")
        content = self._conversation_content()
        self.assertIn("in window message", content)

    def test_after_silent_excluded(self):
        """Messages after 'chistory silent' must not appear in the conversation file."""
        self.repo.commit(message="add feature")
        content = self._conversation_content()
        self.assertNotIn(
            "after silent message",
            content,
            "Hook captured messages recorded after 'chistory silent'",
        )


class TestMultipleListenSilentCycles(unittest.TestCase):
    """listen → msg → silent → msg → listen → msg → silent → msg:
    only the two messages inside a listen/silent window should appear."""

    # Timeline (all 2026-03-15):
    #   listen1=09:00  msg1=10:00  silent1=11:00
    #   msg2=12:00
    #   listen2=13:00  msg3=14:00  silent2=15:00
    #   msg4=16:00

    def setUp(self):
        self.repo = IsolatedRepo()
        self.repo.setup()

        encoded = self.repo.path.replace("/", "-")
        self.stub_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects", encoded)
        os.makedirs(self.stub_dir, exist_ok=True)
        self.stub_jsonl = os.path.join(self.stub_dir, "stub.jsonl")

        stub_lines = [
            # msg1 — inside window 1
            '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"window one message"},"uuid":"u1","timestamp":"2026-03-15T10:00:00.000Z"}',
            '{"parentUuid":"u1","isSidechain":false,"type":"assistant","message":{"id":"m1","role":"assistant","content":[{"type":"text","text":"ack one"}],"stop_reason":"end_turn"},"uuid":"u2","timestamp":"2026-03-15T10:00:01.000Z"}',
            # msg2 — between windows
            '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"between windows message"},"uuid":"u3","timestamp":"2026-03-15T12:00:00.000Z"}',
            '{"parentUuid":"u3","isSidechain":false,"type":"assistant","message":{"id":"m3","role":"assistant","content":[{"type":"text","text":"ack two"}],"stop_reason":"end_turn"},"uuid":"u4","timestamp":"2026-03-15T12:00:01.000Z"}',
            # msg3 — inside window 2
            '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"window two message"},"uuid":"u5","timestamp":"2026-03-15T14:00:00.000Z"}',
            '{"parentUuid":"u5","isSidechain":false,"type":"assistant","message":{"id":"m5","role":"assistant","content":[{"type":"text","text":"ack three"}],"stop_reason":"end_turn"},"uuid":"u6","timestamp":"2026-03-15T14:00:01.000Z"}',
            # msg4 — after both windows
            '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"after all windows message"},"uuid":"u7","timestamp":"2026-03-15T16:00:00.000Z"}',
            '{"parentUuid":"u7","isSidechain":false,"type":"assistant","message":{"id":"m7","role":"assistant","content":[{"type":"text","text":"ack four"}],"stop_reason":"end_turn"},"uuid":"u8","timestamp":"2026-03-15T16:00:01.000Z"}',
        ]
        with open(self.stub_jsonl, "w") as f:
            f.write("\n".join(stub_lines) + "\n")

        def ts(hour):
            return datetime(2026, 3, 15, hour, 0, 0, tzinfo=timezone.utc).timestamp()

        # Simulate: listen → silent → listen → silent (each call appends a timestamp)
        self._append_marker(".claude-history/.listen", ts(9))
        self._append_marker(".claude-history/.silent", ts(11))
        self._append_marker(".claude-history/.listen", ts(13))
        self._append_marker(".claude-history/.silent", ts(15))

    def _append_marker(self, rel_path, ts_val):
        path = os.path.join(self.repo.path, rel_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(str(ts_val) + "\n")

    def tearDown(self):
        if os.path.exists(self.stub_jsonl):
            os.remove(self.stub_jsonl)
        try:
            os.rmdir(self.stub_dir)
        except OSError:
            pass
        self.repo.teardown()

    def _conversation_content(self):
        for f in self.repo.committed_files():
            if f.startswith(".claude-history/") and f.endswith(".md"):
                with open(os.path.join(self.repo.path, f)) as fh:
                    return fh.read()
        return ""

    def test_both_window_messages_included(self):
        """Messages from both listen/silent windows must appear."""
        self.repo.commit(message="add feature")
        content = self._conversation_content()
        self.assertIn("window one message", content)
        self.assertIn("window two message", content)

    def test_between_windows_excluded(self):
        """Messages between the two windows must not appear."""
        self.repo.commit(message="add feature")
        content = self._conversation_content()
        self.assertNotIn("between windows message", content)

    def test_after_all_windows_excluded(self):
        """Messages after the final silent must not appear."""
        self.repo.commit(message="add feature")
        content = self._conversation_content()
        self.assertNotIn("after all windows message", content)


class TestListenSilentEdgeCases(unittest.TestCase):
    """Edge cases involving silents that precede listens or consecutive same-type calls."""

    # All events on 2026-03-15; message always at hour 6.
    MSG_TS = "2026-03-15T06:00:00.000Z"

    def setUp(self):
        self.repo = IsolatedRepo()
        self.repo.setup()

        encoded = self.repo.path.replace("/", "-")
        self.stub_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects", encoded)
        os.makedirs(self.stub_dir, exist_ok=True)
        self.stub_jsonl = os.path.join(self.stub_dir, "stub.jsonl")

        stub_lines = [
            f'{{"parentUuid":null,"isSidechain":false,"type":"user","message":{{"role":"user","content":"the message"}},"uuid":"u1","timestamp":"{self.MSG_TS}"}}',
            f'{{"parentUuid":"u1","isSidechain":false,"type":"assistant","message":{{"id":"m1","role":"assistant","content":[{{"type":"text","text":"ack"}}],"stop_reason":"end_turn"}},"uuid":"u2","timestamp":"{self.MSG_TS}"}}',
        ]
        with open(self.stub_jsonl, "w") as f:
            f.write("\n".join(stub_lines) + "\n")

    def tearDown(self):
        if os.path.exists(self.stub_jsonl):
            os.remove(self.stub_jsonl)
        try:
            os.rmdir(self.stub_dir)
        except OSError:
            pass
        self.repo.teardown()

    def _ts(self, hour):
        return datetime(2026, 3, 15, hour, 0, 0, tzinfo=timezone.utc).timestamp()

    def _append_marker(self, rel_path, ts_val):
        path = os.path.join(self.repo.path, rel_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(str(ts_val) + "\n")

    def _conversation_content(self):
        for f in self.repo.committed_files():
            if f.startswith(".claude-history/") and f.endswith(".md"):
                with open(os.path.join(self.repo.path, f)) as fh:
                    return fh.read()
        return ""

    def test_silent_silent_message_excluded(self):
        """silent → silent → message: no listen ever called, message must not appear."""
        self._append_marker(".claude-history/.silent", self._ts(1))
        self._append_marker(".claude-history/.silent", self._ts(2))
        # message at hour 6
        self.repo.commit(message="add feature")
        self.assertNotIn("the message", self._conversation_content())

    def test_silent_listen_listen_silent_message_excluded(self):
        """silent → listen → listen → silent → message: message is after the closing silent."""
        self._append_marker(".claude-history/.silent", self._ts(1))
        self._append_marker(".claude-history/.listen", self._ts(2))
        self._append_marker(".claude-history/.listen", self._ts(3))
        self._append_marker(".claude-history/.silent", self._ts(4))
        # message at hour 6, after the window closed at hour 4
        self.repo.commit(message="add feature")
        self.assertNotIn("the message", self._conversation_content())

    def test_silent_silent_listen_message_included(self):
        """silent → silent → listen → message: silents precede the listen, window is open."""
        self._append_marker(".claude-history/.silent", self._ts(1))
        self._append_marker(".claude-history/.silent", self._ts(2))
        self._append_marker(".claude-history/.listen", self._ts(3))
        # message at hour 6, inside the open window
        self.repo.commit(message="add feature")
        self.assertIn("the message", self._conversation_content())

    def test_silent_listen_listen_message_included(self):
        """silent → listen → listen → message: window opened at first listen, still open."""
        self._append_marker(".claude-history/.silent", self._ts(1))
        self._append_marker(".claude-history/.listen", self._ts(2))
        self._append_marker(".claude-history/.listen", self._ts(3))
        # message at hour 6, inside the open window
        self.repo.commit(message="add feature")
        self.assertIn("the message", self._conversation_content())


class TestSilentByDefault(unittest.TestCase):
    """Without any configuration, chistory should be silent even when conversation data exists."""

    def setUp(self):
        self.repo = IsolatedRepo()
        self.repo.setup()

        # Compute the encoded path the same way main.py does
        encoded = self.repo.path.replace("/", "-")
        self.stub_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects", encoded)
        os.makedirs(self.stub_dir, exist_ok=True)
        self.stub_jsonl = os.path.join(self.stub_dir, "stub.jsonl")

        # Write a minimal stub conversation with a timestamp well after epoch
        stub_lines = [
            '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"fix the bug"},"uuid":"aaaa-1111","timestamp":"2026-03-15T10:00:00.000Z"}',
            '{"parentUuid":"aaaa-1111","isSidechain":false,"type":"assistant","message":{"id":"msg_stub","role":"assistant","content":[{"type":"text","text":"Done."}],"stop_reason":"end_turn"},"uuid":"bbbb-2222","timestamp":"2026-03-15T10:00:01.000Z"}',
        ]
        with open(self.stub_jsonl, "w") as f:
            f.write("\n".join(stub_lines) + "\n")

    def tearDown(self):
        if os.path.exists(self.stub_jsonl):
            os.remove(self.stub_jsonl)
        try:
            os.rmdir(self.stub_dir)
        except OSError:
            pass
        self.repo.teardown()

    def test_commit_message_not_amended_by_default(self):
        """Hook should not amend the commit message when no opt-in is configured."""
        self.repo.commit(message="add feature")
        msg = self.repo.commit_message()
        self.assertEqual(
            msg,
            "add feature",
            f"Hook amended commit message without configuration: {msg!r}",
        )

    def test_no_conversation_file_written_by_default(self):
        """Hook should not write a conversation .md file when no opt-in is configured."""
        self.repo.commit(message="add feature")
        files = self.repo.committed_files()
        self.assertEqual(
            files,
            ["file.txt"],
            f"Hook wrote conversation file(s) without configuration: {files}",
        )


class TestListenCommand(unittest.TestCase):
    """Tests for the 'chistory listen' opt-in behavior."""

    # Stub conversation messages are timestamped at this moment.
    STUB_TS = "2026-03-15T10:00:00.000Z"
    # A timestamp clearly after the stub messages (same day, evening).
    AFTER_STUB_TS = datetime(2026, 3, 15, 23, 0, 0, tzinfo=timezone.utc).timestamp()

    def setUp(self):
        self.repo = IsolatedRepo()
        self.repo.setup()

        encoded = self.repo.path.replace("/", "-")
        self.stub_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects", encoded)
        os.makedirs(self.stub_dir, exist_ok=True)
        self.stub_jsonl = os.path.join(self.stub_dir, "stub.jsonl")

        stub_lines = [
            f'{{"parentUuid":null,"isSidechain":false,"type":"user","message":{{"role":"user","content":"fix the bug"}},"uuid":"aaaa-1111","timestamp":"{self.STUB_TS}"}}',
            f'{{"parentUuid":"aaaa-1111","isSidechain":false,"type":"assistant","message":{{"id":"msg_stub","role":"assistant","content":[{{"type":"text","text":"Done."}}],"stop_reason":"end_turn"}},"uuid":"bbbb-2222","timestamp":"{self.STUB_TS}"}}',
        ]
        with open(self.stub_jsonl, "w") as f:
            f.write("\n".join(stub_lines) + "\n")

    def tearDown(self):
        if os.path.exists(self.stub_jsonl):
            os.remove(self.stub_jsonl)
        try:
            os.rmdir(self.stub_dir)
        except OSError:
            pass
        self.repo.teardown()

    def _write_listen(self, ts: float) -> None:
        listen_path = os.path.join(self.repo.path, ".claude-history", ".listen")
        os.makedirs(os.path.dirname(listen_path), exist_ok=True)
        with open(listen_path, "w") as f:
            f.write(str(ts))

    def test_listen_enables_hook(self):
        """After 'chistory listen', commits with conversation data should be amended."""
        # Listen timestamp well before the stub messages so they qualify.
        self._write_listen(1.0)
        self.repo.commit(message="add feature")
        msg = self.repo.commit_message()
        self.assertIn(
            "Conversation history:",
            msg,
            f"Hook did not amend commit after listen: {msg!r}",
        )

    def test_messages_before_listen_excluded(self):
        """Messages recorded before 'chistory listen' should not appear in history."""
        # Listen timestamp is after the stub messages, so nothing qualifies.
        self._write_listen(self.AFTER_STUB_TS)
        self.repo.commit(message="add feature")
        msg = self.repo.commit_message()
        self.assertEqual(
            msg,
            "add feature",
            f"Hook included pre-listen messages: {msg!r}",
        )


class TestInitialCommitUnedited(unittest.TestCase):
    """When there is no conversation history, an initial commit should be left unedited."""

    def setUp(self):
        self.repo = IsolatedRepo()
        self.repo.setup()

    def tearDown(self):
        self.repo.teardown()

    def test_commit_message_not_amended(self):
        """Commit message must not have anything appended by the hook."""
        self.repo.commit(message="initial commit")
        msg = self.repo.commit_message()
        self.assertEqual(
            msg,
            "initial commit",
            f"Commit message was amended unexpectedly: {msg!r}",
        )

    def test_no_extra_files_added(self):
        """No extra files (e.g. an empty .md) should be added to the commit."""
        self.repo.commit(message="initial commit")
        files = self.repo.committed_files()
        self.assertEqual(
            files,
            ["file.txt"],
            f"Hook added unexpected files to the commit: {files}",
        )


class TestPreview(unittest.TestCase):
    """Tests for 'chistory preview' — prints exactly what would be written to the .md file."""

    # Far-future timestamp so stub messages are always after any real git commit.
    STUB_TS = "2099-01-01T00:00:00.000Z"

    def setUp(self):
        self.repo = IsolatedRepo()
        self.repo.setup()

        encoded = self.repo.path.replace("/", "-")
        self.stub_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects", encoded)
        os.makedirs(self.stub_dir, exist_ok=True)
        self.stub_jsonl = os.path.join(self.stub_dir, "stub.jsonl")

    def tearDown(self):
        if os.path.exists(self.stub_jsonl):
            os.remove(self.stub_jsonl)
        try:
            os.rmdir(self.stub_dir)
        except OSError:
            pass
        self.repo.teardown()

    def _write_listen(self, ts_val=1.0):
        path = os.path.join(self.repo.path, ".claude-history", ".listen")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(str(ts_val) + "\n")

    def _write_stub(self, user_text="fix the widget", assistant_text="Done."):
        stub_lines = [
            f'{{"parentUuid":null,"isSidechain":false,"type":"user","message":{{"role":"user","content":"{user_text}"}},"uuid":"p1","timestamp":"{self.STUB_TS}"}}',
            f'{{"parentUuid":"p1","isSidechain":false,"type":"assistant","message":{{"id":"pm1","role":"assistant","content":[{{"type":"text","text":"{assistant_text}"}}],"stop_reason":"end_turn"}},"uuid":"p2","timestamp":"{self.STUB_TS}"}}',
        ]
        with open(self.stub_jsonl, "w") as f:
            f.write("\n".join(stub_lines) + "\n")

    def _run_preview(self):
        result = subprocess.run(
            [sys.executable, MAIN_PY, "preview"],
            cwd=self.repo.path,
            capture_output=True,
            text=True,
        )
        return result.stdout

    def test_preview_output_matches_md_format(self):
        """Preview output must be identical in structure to what would be written to the .md file."""
        self.repo.commit(message="initial")
        self._write_listen()
        self._write_stub(user_text="fix the widget", assistant_text="Done.")
        output = self._run_preview()
        self.assertIn("# Conversation since last commit", output)
        self.assertIn("### User", output)
        self.assertIn("fix the widget", output)
        self.assertIn("### Assistant", output)
        self.assertIn("Done.", output)

    def test_preview_not_listening(self):
        """Preview with no listen file should report not listening."""
        self.repo.commit(message="initial")
        self._write_stub()
        output = self._run_preview()
        self.assertNotIn("# Conversation since last commit", output)

    def test_preview_no_pending_messages(self):
        """Preview with listen but no new messages should not print conversation header."""
        self.repo.commit(message="initial")
        self._write_listen()
        # no stub JSONL written
        output = self._run_preview()
        self.assertNotIn("# Conversation since last commit", output)


class TestPreviewFull(unittest.TestCase):
    """Tests for 'chistory preview --full' — shows all messages since last commit
    with CH[N-o] (included) or CH[N-x] (excluded) markers per message."""

    # Timeline (all 2099-01-01):
    #   before-window user+assistant at 01:00/01:01
    #   listen at 01:30
    #   inside-window user+assistant at 02:00/02:01
    #   silent at 02:30
    #   after-window user+assistant at 03:00/03:01
    LISTEN_TS = datetime(2099, 1, 1, 1, 30, 0, tzinfo=timezone.utc).timestamp()
    SILENT_TS = datetime(2099, 1, 1, 2, 30, 0, tzinfo=timezone.utc).timestamp()

    def setUp(self):
        self.repo = IsolatedRepo()
        self.repo.setup()

        encoded = self.repo.path.replace("/", "-")
        self.stub_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects", encoded)
        os.makedirs(self.stub_dir, exist_ok=True)
        self.stub_jsonl = os.path.join(self.stub_dir, "stub.jsonl")

        stub_lines = [
            # before window
            '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"before window"},"uuid":"u1","timestamp":"2099-01-01T01:00:00.000Z"}',
            '{"parentUuid":"u1","isSidechain":false,"type":"assistant","message":{"id":"m1","role":"assistant","content":[{"type":"text","text":"ack before"}],"stop_reason":"end_turn"},"uuid":"u2","timestamp":"2099-01-01T01:00:01.000Z"}',
            # inside window
            '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"inside window"},"uuid":"u3","timestamp":"2099-01-01T02:00:00.000Z"}',
            '{"parentUuid":"u3","isSidechain":false,"type":"assistant","message":{"id":"m3","role":"assistant","content":[{"type":"text","text":"ack inside"}],"stop_reason":"end_turn"},"uuid":"u4","timestamp":"2099-01-01T02:00:01.000Z"}',
            # after window
            '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"after window"},"uuid":"u5","timestamp":"2099-01-01T03:00:00.000Z"}',
            '{"parentUuid":"u5","isSidechain":false,"type":"assistant","message":{"id":"m5","role":"assistant","content":[{"type":"text","text":"ack after"}],"stop_reason":"end_turn"},"uuid":"u6","timestamp":"2099-01-01T03:00:01.000Z"}',
        ]
        with open(self.stub_jsonl, "w") as f:
            f.write("\n".join(stub_lines) + "\n")

        listen_path = os.path.join(self.repo.path, ".claude-history", ".listen")
        os.makedirs(os.path.dirname(listen_path), exist_ok=True)
        with open(listen_path, "w") as f:
            f.write(str(self.LISTEN_TS) + "\n")
        silent_path = os.path.join(self.repo.path, ".claude-history", ".silent")
        with open(silent_path, "w") as f:
            f.write(str(self.SILENT_TS) + "\n")

    def tearDown(self):
        if os.path.exists(self.stub_jsonl):
            os.remove(self.stub_jsonl)
        try:
            os.rmdir(self.stub_dir)
        except OSError:
            pass
        self.repo.teardown()

    def _run_full_preview(self):
        result = subprocess.run(
            [sys.executable, MAIN_PY, "preview", "--full"],
            cwd=self.repo.path,
            capture_output=True,
            text=True,
        )
        return result.stdout

    def test_excluded_messages_marked_with_x(self):
        """Messages outside all listen/silent windows get CH[N-x]."""
        self.repo.commit(message="initial")
        output = self._run_full_preview()
        # before-window messages are entries 1 and 2
        self.assertIn("CH[1-x]", output)
        self.assertIn("CH[2-x]", output)

    def test_included_messages_marked_with_o(self):
        """Messages inside a listen/silent window get CH[N-o]."""
        self.repo.commit(message="initial")
        output = self._run_full_preview()
        # inside-window messages are entries 3 and 4
        self.assertIn("CH[3-o]", output)
        self.assertIn("CH[4-o]", output)

    def test_after_window_messages_marked_with_x(self):
        """Messages after the silent get CH[N-x]."""
        self.repo.commit(message="initial")
        output = self._run_full_preview()
        self.assertIn("CH[5-x]", output)
        self.assertIn("CH[6-x]", output)

    def test_message_content_present(self):
        """All message contents appear in the output regardless of inclusion."""
        self.repo.commit(message="initial")
        output = self._run_full_preview()
        self.assertIn("before window", output)
        self.assertIn("inside window", output)
        self.assertIn("after window", output)


class TestIncludeCommand(unittest.TestCase):
    """Tests for 'chistory include N' — force-includes a CH[N-x] message by its preview-full id.

    Timeline (all 2099-01-01):
      before-window user+assistant at 01:00/01:01  → CH[1-x], CH[2-x]
      listen at 01:30
      inside-window user+assistant at 02:00/02:01  → CH[3-o], CH[4-o]
      silent at 02:30
      after-window user+assistant at 03:00/03:01   → CH[5-x], CH[6-x]
    """

    LISTEN_TS = datetime(2099, 1, 1, 1, 30, 0, tzinfo=timezone.utc).timestamp()
    SILENT_TS = datetime(2099, 1, 1, 2, 30, 0, tzinfo=timezone.utc).timestamp()

    def setUp(self):
        self.repo = IsolatedRepo()
        self.repo.setup()

        encoded = self.repo.path.replace("/", "-")
        self.stub_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects", encoded)
        os.makedirs(self.stub_dir, exist_ok=True)
        self.stub_jsonl = os.path.join(self.stub_dir, "stub.jsonl")

        stub_lines = [
            # before window (CH[1-x], CH[2-x])
            '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"before window message"},"uuid":"u1","timestamp":"2099-01-01T01:00:00.000Z"}',
            '{"parentUuid":"u1","isSidechain":false,"type":"assistant","message":{"id":"m1","role":"assistant","content":[{"type":"text","text":"ack before"}],"stop_reason":"end_turn"},"uuid":"u2","timestamp":"2099-01-01T01:00:01.000Z"}',
            # inside window (CH[3-o], CH[4-o])
            '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"inside window message"},"uuid":"u3","timestamp":"2099-01-01T02:00:00.000Z"}',
            '{"parentUuid":"u3","isSidechain":false,"type":"assistant","message":{"id":"m3","role":"assistant","content":[{"type":"text","text":"ack inside"}],"stop_reason":"end_turn"},"uuid":"u4","timestamp":"2099-01-01T02:00:01.000Z"}',
            # after window (CH[5-x], CH[6-x])
            '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"after window message"},"uuid":"u5","timestamp":"2099-01-01T03:00:00.000Z"}',
            '{"parentUuid":"u5","isSidechain":false,"type":"assistant","message":{"id":"m5","role":"assistant","content":[{"type":"text","text":"ack after"}],"stop_reason":"end_turn"},"uuid":"u6","timestamp":"2099-01-01T03:00:01.000Z"}',
        ]
        with open(self.stub_jsonl, "w") as f:
            f.write("\n".join(stub_lines) + "\n")

        listen_path = os.path.join(self.repo.path, ".claude-history", ".listen")
        os.makedirs(os.path.dirname(listen_path), exist_ok=True)
        with open(listen_path, "w") as f:
            f.write(str(self.LISTEN_TS) + "\n")
        silent_path = os.path.join(self.repo.path, ".claude-history", ".silent")
        with open(silent_path, "w") as f:
            f.write(str(self.SILENT_TS) + "\n")

        # Make an initial commit so HEAD exists before tests run preview/commit logic
        self.repo.commit(message="initial")

    def tearDown(self):
        if os.path.exists(self.stub_jsonl):
            os.remove(self.stub_jsonl)
        try:
            os.rmdir(self.stub_dir)
        except OSError:
            pass
        self.repo.teardown()

    def _run(self, *args):
        result = subprocess.run(
            [sys.executable, MAIN_PY] + list(args),
            cwd=self.repo.path,
            capture_output=True,
            text=True,
        )
        return result

    def _run_preview(self):
        return self._run("preview").stdout

    def _run_preview_full(self):
        return self._run("preview", "--full").stdout

    def _conversation_content(self):
        for f in self.repo.committed_files():
            if f.startswith(".claude-history/") and f.endswith(".md"):
                with open(os.path.join(self.repo.path, f)) as fh:
                    return fh.read()
        return ""

    def test_excluded_message_appears_in_preview_after_include(self):
        """After 'chistory include 1', the before-window user message should appear in preview."""
        self._run("include", "1")
        output = self._run_preview()
        self.assertIn("before window message", output)

    def test_included_message_appears_in_commit(self):
        """After 'chistory include 1', the before-window message should appear in the committed .md."""
        self._run("include", "1")
        self.repo.commit(message="add feature", content="v2\n")
        content = self._conversation_content()
        self.assertIn("before window message", content)

    def test_preview_full_shows_o_marker_after_include(self):
        """After 'chistory include 1', preview --full should show CH[1-o] instead of CH[1-x]."""
        self._run("include", "1")
        output = self._run_preview_full()
        self.assertIn("CH[1-o]", output)
        self.assertNotIn("CH[1-x]", output)

    def test_other_excluded_messages_still_excluded_after_include(self):
        """Including message 1 should not cause other excluded messages (e.g. 5, 6) to appear."""
        self._run("include", "1")
        output = self._run_preview()
        self.assertNotIn("after window message", output)

    def test_include_already_included_message_is_idempotent(self):
        """Including an already-included message (CH[3-o]) must not raise an error."""
        result = self._run("include", "3")
        self.assertEqual(result.returncode, 0)
        output = self._run_preview()
        self.assertIn("inside window message", output)

    def test_include_invalid_id_exits_with_error(self):
        """'chistory include 999' (out of range) should exit non-zero and print an error."""
        result = self._run("include", "999")
        self.assertNotEqual(result.returncode, 0)

    def test_multiple_includes_all_appear_in_preview(self):
        """Including both message 1 and 5 should make both appear in preview."""
        self._run("include", "1")
        self._run("include", "5")
        output = self._run_preview()
        self.assertIn("before window message", output)
        self.assertIn("after window message", output)


class TestExcludeCommand(unittest.TestCase):
    """Tests for 'chistory exclude N' — force-excludes a CH[N-o] message by its preview-full id,
    and for 'chistory include N' re-including a previously excluded message.

    Timeline (all 2099-01-01):
      before-window user+assistant at 01:00/01:01  → CH[1-x], CH[2-x]
      listen at 01:30
      inside-window user+assistant at 02:00/02:01  → CH[3-o], CH[4-o]
      silent at 02:30
      after-window user+assistant at 03:00/03:01   → CH[5-x], CH[6-x]
    """

    LISTEN_TS = datetime(2099, 1, 1, 1, 30, 0, tzinfo=timezone.utc).timestamp()
    SILENT_TS = datetime(2099, 1, 1, 2, 30, 0, tzinfo=timezone.utc).timestamp()

    def setUp(self):
        self.repo = IsolatedRepo()
        self.repo.setup()

        encoded = self.repo.path.replace("/", "-")
        self.stub_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects", encoded)
        os.makedirs(self.stub_dir, exist_ok=True)
        self.stub_jsonl = os.path.join(self.stub_dir, "stub.jsonl")

        stub_lines = [
            # before window (CH[1-x], CH[2-x])
            '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"before window message"},"uuid":"u1","timestamp":"2099-01-01T01:00:00.000Z"}',
            '{"parentUuid":"u1","isSidechain":false,"type":"assistant","message":{"id":"m1","role":"assistant","content":[{"type":"text","text":"ack before"}],"stop_reason":"end_turn"},"uuid":"u2","timestamp":"2099-01-01T01:00:01.000Z"}',
            # inside window (CH[3-o], CH[4-o])
            '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"inside window message"},"uuid":"u3","timestamp":"2099-01-01T02:00:00.000Z"}',
            '{"parentUuid":"u3","isSidechain":false,"type":"assistant","message":{"id":"m3","role":"assistant","content":[{"type":"text","text":"ack inside"}],"stop_reason":"end_turn"},"uuid":"u4","timestamp":"2099-01-01T02:00:01.000Z"}',
            # after window (CH[5-x], CH[6-x])
            '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"after window message"},"uuid":"u5","timestamp":"2099-01-01T03:00:00.000Z"}',
            '{"parentUuid":"u5","isSidechain":false,"type":"assistant","message":{"id":"m5","role":"assistant","content":[{"type":"text","text":"ack after"}],"stop_reason":"end_turn"},"uuid":"u6","timestamp":"2099-01-01T03:00:01.000Z"}',
        ]
        with open(self.stub_jsonl, "w") as f:
            f.write("\n".join(stub_lines) + "\n")

        listen_path = os.path.join(self.repo.path, ".claude-history", ".listen")
        os.makedirs(os.path.dirname(listen_path), exist_ok=True)
        with open(listen_path, "w") as f:
            f.write(str(self.LISTEN_TS) + "\n")
        silent_path = os.path.join(self.repo.path, ".claude-history", ".silent")
        with open(silent_path, "w") as f:
            f.write(str(self.SILENT_TS) + "\n")

        self.repo.commit(message="initial")

    def tearDown(self):
        if os.path.exists(self.stub_jsonl):
            os.remove(self.stub_jsonl)
        try:
            os.rmdir(self.stub_dir)
        except OSError:
            pass
        self.repo.teardown()

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, MAIN_PY] + list(args),
            cwd=self.repo.path,
            capture_output=True,
            text=True,
        )

    def _run_preview(self):
        return self._run("preview").stdout

    def _run_preview_full(self):
        return self._run("preview", "--full").stdout

    def _conversation_content(self):
        for f in self.repo.committed_files():
            if f.startswith(".claude-history/") and f.endswith(".md"):
                with open(os.path.join(self.repo.path, f)) as fh:
                    return fh.read()
        return ""

    # --- exclude a window-included message ---

    def test_exclude_removes_in_window_message_from_preview(self):
        """After 'chistory exclude 3', the inside-window user message should not appear in preview."""
        self._run("exclude", "3")
        output = self._run_preview()
        self.assertNotIn("inside window message", output)

    def test_exclude_marks_previously_included_as_x_in_preview_full(self):
        """After 'chistory exclude 3', preview --full should show CH[3-x] instead of CH[3-o]."""
        self._run("exclude", "3")
        output = self._run_preview_full()
        self.assertIn("CH[3-x]", output)
        self.assertNotIn("CH[3-o]", output)

    def test_exclude_does_not_affect_commit_content(self):
        """After 'chistory exclude 3', the inside-window message must not appear in the committed .md."""
        self._run("exclude", "3")
        self.repo.commit(message="add feature", content="v2\n")
        content = self._conversation_content()
        self.assertNotIn("inside window message", content)

    def test_exclude_other_included_messages_unaffected(self):
        """Excluding message 3 must not affect message 4 (the assistant reply also in window)."""
        self._run("exclude", "3")
        output = self._run_preview()
        self.assertIn("ack inside", output)

    # --- exclude a force-included message ---

    def test_exclude_cancels_previous_include(self):
        """'include 1' then 'exclude 1': message should no longer appear in preview."""
        self._run("include", "1")
        self._run("exclude", "1")
        output = self._run_preview()
        self.assertNotIn("before window message", output)

    def test_exclude_of_force_included_shows_x_in_preview_full(self):
        """'include 1' then 'exclude 1': preview --full must show CH[1-x]."""
        self._run("include", "1")
        self._run("exclude", "1")
        output = self._run_preview_full()
        self.assertIn("CH[1-x]", output)
        self.assertNotIn("CH[1-o]", output)

    # --- include re-includes a previously excluded message ---

    def test_include_after_exclude_restores_window_message(self):
        """'exclude 3' then 'include 3': the inside-window message should reappear in preview."""
        self._run("exclude", "3")
        self._run("include", "3")
        output = self._run_preview()
        self.assertIn("inside window message", output)

    def test_include_after_exclude_shows_o_in_preview_full(self):
        """'exclude 3' then 'include 3': preview --full should show CH[3-o]."""
        self._run("exclude", "3")
        self._run("include", "3")
        output = self._run_preview_full()
        self.assertIn("CH[3-o]", output)
        self.assertNotIn("CH[3-x]", output)

    # --- invalid id ---

    def test_include_exclude_include_shows_message(self):
        """include → exclude → include: message must appear in preview."""
        self._run("include", "1")
        self._run("exclude", "1")
        self._run("include", "1")
        output = self._run_preview()
        self.assertIn("before window message", output)

    def test_exclude_include_exclude_hides_message(self):
        """exclude → include → exclude: message must not appear in preview."""
        self._run("exclude", "3")
        self._run("include", "3")
        self._run("exclude", "3")
        output = self._run_preview()
        self.assertNotIn("inside window message", output)

    def test_include_exclude_include_exclude_hides_message(self):
        """include → exclude → include → exclude: message must not appear in preview."""
        self._run("include", "1")
        self._run("exclude", "1")
        self._run("include", "1")
        self._run("exclude", "1")
        output = self._run_preview()
        self.assertNotIn("before window message", output)

    def test_exclude_include_exclude_include_shows_message(self):
        """exclude → include → exclude → include: message must appear in preview."""
        self._run("exclude", "3")
        self._run("include", "3")
        self._run("exclude", "3")
        self._run("include", "3")
        output = self._run_preview()
        self.assertIn("inside window message", output)

    def test_exclude_invalid_id_exits_with_error(self):
        """'chistory exclude 999' should exit non-zero."""
        result = self._run("exclude", "999")
        self.assertNotEqual(result.returncode, 0)


class TestIncludeExcludeRange(unittest.TestCase):
    """Tests for 'chistory include x..y' and 'chistory exclude x..y' range syntax.

    Timeline (all 2099-01-01):
      before-window user+assistant at 01:00/01:01  → CH[1-x], CH[2-x]
      listen at 01:30
      inside-window user+assistant at 02:00/02:01  → CH[3-o], CH[4-o]
      silent at 02:30
      after-window user+assistant at 03:00/03:01   → CH[5-x], CH[6-x]
    """

    LISTEN_TS = datetime(2099, 1, 1, 1, 30, 0, tzinfo=timezone.utc).timestamp()
    SILENT_TS = datetime(2099, 1, 1, 2, 30, 0, tzinfo=timezone.utc).timestamp()

    def setUp(self):
        self.repo = IsolatedRepo()
        self.repo.setup()

        encoded = self.repo.path.replace("/", "-")
        self.stub_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects", encoded)
        os.makedirs(self.stub_dir, exist_ok=True)
        self.stub_jsonl = os.path.join(self.stub_dir, "stub.jsonl")

        stub_lines = [
            # before window (CH[1-x], CH[2-x])
            '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"before window message"},"uuid":"u1","timestamp":"2099-01-01T01:00:00.000Z"}',
            '{"parentUuid":"u1","isSidechain":false,"type":"assistant","message":{"id":"m1","role":"assistant","content":[{"type":"text","text":"ack before"}],"stop_reason":"end_turn"},"uuid":"u2","timestamp":"2099-01-01T01:00:01.000Z"}',
            # inside window (CH[3-o], CH[4-o])
            '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"inside window message"},"uuid":"u3","timestamp":"2099-01-01T02:00:00.000Z"}',
            '{"parentUuid":"u3","isSidechain":false,"type":"assistant","message":{"id":"m3","role":"assistant","content":[{"type":"text","text":"ack inside"}],"stop_reason":"end_turn"},"uuid":"u4","timestamp":"2099-01-01T02:00:01.000Z"}',
            # after window (CH[5-x], CH[6-x])
            '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"after window message"},"uuid":"u5","timestamp":"2099-01-01T03:00:00.000Z"}',
            '{"parentUuid":"u5","isSidechain":false,"type":"assistant","message":{"id":"m5","role":"assistant","content":[{"type":"text","text":"ack after"}],"stop_reason":"end_turn"},"uuid":"u6","timestamp":"2099-01-01T03:00:01.000Z"}',
        ]
        with open(self.stub_jsonl, "w") as f:
            f.write("\n".join(stub_lines) + "\n")

        listen_path = os.path.join(self.repo.path, ".claude-history", ".listen")
        os.makedirs(os.path.dirname(listen_path), exist_ok=True)
        with open(listen_path, "w") as f:
            f.write(str(self.LISTEN_TS) + "\n")
        silent_path = os.path.join(self.repo.path, ".claude-history", ".silent")
        with open(silent_path, "w") as f:
            f.write(str(self.SILENT_TS) + "\n")

        self.repo.commit(message="initial")

    def tearDown(self):
        if os.path.exists(self.stub_jsonl):
            os.remove(self.stub_jsonl)
        try:
            os.rmdir(self.stub_dir)
        except OSError:
            pass
        self.repo.teardown()

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, MAIN_PY] + list(args),
            cwd=self.repo.path,
            capture_output=True,
            text=True,
        )

    def _run_preview(self):
        return self._run("preview").stdout

    def _run_preview_full(self):
        return self._run("preview", "--full").stdout

    # --- include range ---

    def test_include_range_all_messages_appear_in_preview(self):
        """'chistory include 1..6' should make all messages appear in preview."""
        self._run("include", "1..6")
        output = self._run_preview()
        self.assertIn("before window message", output)
        self.assertIn("inside window message", output)
        self.assertIn("after window message", output)

    def test_include_range_endpoints_are_inclusive(self):
        """'chistory include 1..2' includes both endpoint messages."""
        self._run("include", "1..2")
        output = self._run_preview_full()
        self.assertIn("CH[1-o]", output)
        self.assertIn("CH[2-o]", output)
        self.assertNotIn("CH[1-x]", output)
        self.assertNotIn("CH[2-x]", output)

    def test_include_range_outside_only_includes_range(self):
        """'chistory include 1..2' must not pull in messages outside that range."""
        self._run("include", "1..2")
        output = self._run_preview_full()
        # messages 5 and 6 are outside the range and outside any window
        self.assertIn("CH[5-x]", output)
        self.assertIn("CH[6-x]", output)

    # --- exclude range ---

    def test_exclude_range_removes_all_from_preview(self):
        """'chistory exclude 3..4' should remove both in-window messages from preview."""
        self._run("exclude", "3..4")
        output = self._run_preview()
        self.assertNotIn("inside window message", output)
        self.assertNotIn("ack inside", output)

    def test_exclude_range_endpoints_are_inclusive(self):
        """'chistory exclude 3..4' marks both endpoints as x in preview --full."""
        self._run("exclude", "3..4")
        output = self._run_preview_full()
        self.assertIn("CH[3-x]", output)
        self.assertIn("CH[4-x]", output)
        self.assertNotIn("CH[3-o]", output)
        self.assertNotIn("CH[4-o]", output)

    def test_exclude_range_outside_messages_unaffected(self):
        """'chistory exclude 3..4' must not change the status of messages outside that range."""
        self._run("exclude", "3..4")
        output = self._run_preview_full()
        # messages 1 and 2 were already excluded by window, still x
        self.assertIn("CH[1-x]", output)
        self.assertIn("CH[2-x]", output)


class TestIncludeExcludeAll(unittest.TestCase):
    """Tests for 'chistory include --all' and 'chistory exclude --all'.

    Timeline (all 2099-01-01):
      before-window user+assistant at 01:00/01:01  → CH[1-x], CH[2-x]
      listen at 01:30
      inside-window user+assistant at 02:00/02:01  → CH[3-o], CH[4-o]
      silent at 02:30
      after-window user+assistant at 03:00/03:01   → CH[5-x], CH[6-x]
    """

    LISTEN_TS = datetime(2099, 1, 1, 1, 30, 0, tzinfo=timezone.utc).timestamp()
    SILENT_TS = datetime(2099, 1, 1, 2, 30, 0, tzinfo=timezone.utc).timestamp()

    def setUp(self):
        self.repo = IsolatedRepo()
        self.repo.setup()

        encoded = self.repo.path.replace("/", "-")
        self.stub_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects", encoded)
        os.makedirs(self.stub_dir, exist_ok=True)
        self.stub_jsonl = os.path.join(self.stub_dir, "stub.jsonl")

        stub_lines = [
            '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"before window message"},"uuid":"u1","timestamp":"2099-01-01T01:00:00.000Z"}',
            '{"parentUuid":"u1","isSidechain":false,"type":"assistant","message":{"id":"m1","role":"assistant","content":[{"type":"text","text":"ack before"}],"stop_reason":"end_turn"},"uuid":"u2","timestamp":"2099-01-01T01:00:01.000Z"}',
            '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"inside window message"},"uuid":"u3","timestamp":"2099-01-01T02:00:00.000Z"}',
            '{"parentUuid":"u3","isSidechain":false,"type":"assistant","message":{"id":"m3","role":"assistant","content":[{"type":"text","text":"ack inside"}],"stop_reason":"end_turn"},"uuid":"u4","timestamp":"2099-01-01T02:00:01.000Z"}',
            '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"after window message"},"uuid":"u5","timestamp":"2099-01-01T03:00:00.000Z"}',
            '{"parentUuid":"u5","isSidechain":false,"type":"assistant","message":{"id":"m5","role":"assistant","content":[{"type":"text","text":"ack after"}],"stop_reason":"end_turn"},"uuid":"u6","timestamp":"2099-01-01T03:00:01.000Z"}',
        ]
        with open(self.stub_jsonl, "w") as f:
            f.write("\n".join(stub_lines) + "\n")

        listen_path = os.path.join(self.repo.path, ".claude-history", ".listen")
        os.makedirs(os.path.dirname(listen_path), exist_ok=True)
        with open(listen_path, "w") as f:
            f.write(str(self.LISTEN_TS) + "\n")
        silent_path = os.path.join(self.repo.path, ".claude-history", ".silent")
        with open(silent_path, "w") as f:
            f.write(str(self.SILENT_TS) + "\n")

        self.repo.commit(message="initial")

    def tearDown(self):
        if os.path.exists(self.stub_jsonl):
            os.remove(self.stub_jsonl)
        try:
            os.rmdir(self.stub_dir)
        except OSError:
            pass
        self.repo.teardown()

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, MAIN_PY] + list(args),
            cwd=self.repo.path,
            capture_output=True,
            text=True,
        )

    def _run_preview(self):
        return self._run("preview").stdout

    def _run_preview_full(self):
        return self._run("preview", "--full").stdout

    # --- include --all ---

    def test_include_all_shows_every_message_in_preview(self):
        """'chistory include --all' must make all messages appear in preview."""
        self._run("include", "--all")
        output = self._run_preview()
        self.assertIn("before window message", output)
        self.assertIn("inside window message", output)
        self.assertIn("after window message", output)

    def test_include_all_marks_every_message_o_in_preview_full(self):
        """'chistory include --all' must mark every entry as CH[N-o] in preview --full."""
        self._run("include", "--all")
        output = self._run_preview_full()
        for n in range(1, 7):
            self.assertIn(f"CH[{n}-o]", output)
            self.assertNotIn(f"CH[{n}-x]", output)

    # --- exclude --all ---

    def test_exclude_all_hides_every_message_from_preview(self):
        """'chistory exclude --all' must make no messages appear in preview."""
        self._run("exclude", "--all")
        output = self._run_preview()
        self.assertNotIn("before window message", output)
        self.assertNotIn("inside window message", output)
        self.assertNotIn("after window message", output)

    def test_exclude_all_marks_every_message_x_in_preview_full(self):
        """'chistory exclude --all' must mark every entry as CH[N-x] in preview --full."""
        self._run("exclude", "--all")
        output = self._run_preview_full()
        for n in range(1, 7):
            self.assertIn(f"CH[{n}-x]", output)
            self.assertNotIn(f"CH[{n}-o]", output)

    # --- interaction ---

    def test_exclude_all_then_include_one(self):
        """'exclude --all' then 'include 3': only message 3 should appear in preview."""
        self._run("exclude", "--all")
        self._run("include", "3")
        output = self._run_preview()
        self.assertIn("inside window message", output)
        self.assertNotIn("before window message", output)
        self.assertNotIn("after window message", output)

    def test_include_all_then_exclude_one(self):
        """'include --all' then 'exclude 3': message 3 should not appear in preview."""
        self._run("include", "--all")
        self._run("exclude", "3")
        output = self._run_preview()
        self.assertNotIn("inside window message", output)
        self.assertIn("before window message", output)
        self.assertIn("after window message", output)


class TestMultipleCommits(unittest.TestCase):
    """Messages are split correctly across two commits.

    Strategy: message A has a 2020 timestamp (before any real git commit that
    runs during the test) and message B has a 2099 timestamp (always after).

    Timeline:
      listen (ts=1.0)
      message A  (2020-01-01)
      commit 1   → cutoff=0 (no HEAD~1), captures message A
      message B  (2099-01-01) appended to stub
      commit 2   → cutoff=commit_1_ts (~2026), captures message B only
    """

    MSG_A_TS = "2020-01-01T00:00:00.000Z"
    MSG_B_TS = "2099-01-01T00:00:00.000Z"

    def setUp(self):
        self.repo = IsolatedRepo()
        self.repo.setup()

        encoded = self.repo.path.replace("/", "-")
        self.stub_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects", encoded)
        os.makedirs(self.stub_dir, exist_ok=True)
        self.stub_jsonl = os.path.join(self.stub_dir, "stub.jsonl")

        self._write_stub([
            f'{{"parentUuid":null,"isSidechain":false,"type":"user","message":{{"role":"user","content":"first message"}},"uuid":"u1","timestamp":"{self.MSG_A_TS}"}}',
            f'{{"parentUuid":"u1","isSidechain":false,"type":"assistant","message":{{"id":"m1","role":"assistant","content":[{{"type":"text","text":"ack first"}}],"stop_reason":"end_turn"}},"uuid":"u2","timestamp":"{self.MSG_A_TS}"}}',
        ])

        listen_path = os.path.join(self.repo.path, ".claude-history", ".listen")
        os.makedirs(os.path.dirname(listen_path), exist_ok=True)
        with open(listen_path, "w") as f:
            f.write("1.0\n")

    def tearDown(self):
        if os.path.exists(self.stub_jsonl):
            os.remove(self.stub_jsonl)
        try:
            os.rmdir(self.stub_dir)
        except OSError:
            pass
        self.repo.teardown()

    def _write_stub(self, lines, mode="w"):
        with open(self.stub_jsonl, mode) as f:
            f.write("\n".join(lines) + "\n")

    def _append_message_b(self):
        self._write_stub([
            f'{{"parentUuid":null,"isSidechain":false,"type":"user","message":{{"role":"user","content":"second message"}},"uuid":"u3","timestamp":"{self.MSG_B_TS}"}}',
            f'{{"parentUuid":"u3","isSidechain":false,"type":"assistant","message":{{"id":"m3","role":"assistant","content":[{{"type":"text","text":"ack second"}}],"stop_reason":"end_turn"}},"uuid":"u4","timestamp":"{self.MSG_B_TS}"}}',
        ], mode="a")

    def _conversation_content(self):
        for f in self.repo.committed_files():
            if f.startswith(".claude-history/") and f.endswith(".md"):
                with open(os.path.join(self.repo.path, f)) as fh:
                    return fh.read()
        return ""

    def test_first_commit_includes_message_a(self):
        """Commit 1 (no HEAD~1, cutoff=0) should capture message A."""
        self.repo.commit(message="first commit")
        self.assertIn("first message", self._conversation_content())

    def test_second_commit_includes_message_b(self):
        """Commit 2 should capture message B (timestamped after commit 1)."""
        self.repo.commit(message="first commit")
        self._append_message_b()
        self.repo.commit(message="second commit", content="v2\n")
        self.assertIn("second message", self._conversation_content())

    def test_second_commit_excludes_message_a(self):
        """Commit 2 cutoff is commit 1's timestamp, so message A must not reappear."""
        self.repo.commit(message="first commit")
        self._append_message_b()
        self.repo.commit(message="second commit", content="v2\n")
        self.assertNotIn("first message", self._conversation_content())

    def test_second_commit_shows_message_b_despite_earlier_exclude(self):
        """listen → message A → exclude 1 → commit 1 → message B → commit 2.

        The exclude record for message A must not suppress message B in commit 2.
        """
        subprocess.run(
            [sys.executable, MAIN_PY, "exclude", "1"],
            cwd=self.repo.path, capture_output=True, text=True,
        )
        self.repo.commit(message="first commit")
        # Message A was excluded, so commit 1 should have no conversation file
        self.assertEqual("", self._conversation_content())

        self._append_message_b()
        self.repo.commit(message="second commit", content="v2\n")
        self.assertIn("second message", self._conversation_content())


if __name__ == "__main__":
    unittest.main(verbosity=2)
