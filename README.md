# claude-history

Automatically saves your Claude Code conversation history alongside git commits. After each commit, it captures the Claude messages exchanged since the previous commit and appends a Markdown file to your repository.

## How it works

1. After every `git commit`, a post-commit hook runs `main.py --hook`.
2. The hook reads Claude Code's JSONL conversation logs from `~/.claude/projects/`.
3. It writes a Markdown transcript to `.claude-history/<short-hash>.md`.
4. It amends the commit message to include a link to that file.

## Requirements

- Python 3.9+
- Git
- [Claude Code](https://claude.ai/code) (conversations must be recorded in `~/.claude/projects/`)

## Installation

### 1. Clone the repository

```bash
git clone <repo-url>
cd claude-history
```

### 2. Add `chistory` to your PATH

**Unix (macOS/Linux):**

```bash
chmod +x main.py
mkdir -p ~/.local/bin
ln -s "$(pwd)/main.py" ~/.local/bin/chistory
```

Make sure `~/.local/bin` is on your PATH (add `export PATH="$HOME/.local/bin:$PATH"` to your `~/.bashrc` or `~/.zshrc` if needed). You may need to restart your terminal session or run `source ~/.bashrc` (or `source ~/.zshrc`) for the PATH change to take effect.

**Windows (PowerShell):**

```powershell
# Create a wrapper script in a directory that's already on your PATH, e.g. C:\Tools
New-Item -ItemType Directory -Force C:\Tools
Set-Content C:\Tools\chistory.cmd "@python3 `"$((Get-Location).Path)\main.py`" %*"
```

Then add `C:\Tools` to your PATH via System Properties > Environment Variables if it isn't already. You may need to restart your terminal session for the PATH change to take effect.

After setup, use `chistory` from anywhere instead of `python3 /path/to/main.py`.

### 3. Install the hook into a target project

Run the following from inside the git project you want to track:

```bash
chistory --install
```

This writes `.git/hooks/post-commit` in the target project and makes it executable. The hook calls `chistory` on every commit.

To install the hook for the `claude-history` repo itself:

```bash
cd /path/to/claude-history
chistory --install
```

### 4. Verify

Make a commit in the target project. You should see:

```
[claude-history] Saved conversation to .claude-history/<hash>.md
```

The commit message will be amended to include:

```
Conversation history: .claude-history/<hash>.md
```

## Manual usage

To generate `conversation.md` from messages since the last commit without committing:

```bash
chistory
```

This writes `conversation.md` in the current directory.

## Resetting the conversation window

To ignore all conversation history recorded before the current moment:

```bash
chistory --reset
```

This writes a Unix timestamp to `.claude-history/.reset`. On all future runs (both the hook and manual usage), any messages older than this timestamp are excluded. This is useful for starting fresh without losing existing commit history.

The reset file is not committed automatically — add it to your repository if you want the reset to persist for other contributors.

## Output format

Each `.claude-history/<hash>.md` file contains the conversation transcript:

```markdown
# Conversation since last commit

### User

Your message here.

### Assistant

Claude's response here.
```

## File structure

```
your-project/
├── .claude-history/
│   ├── abc12345.md   # conversation for commit abc12345
│   ├── def67890.md   # conversation for commit def67890
│   └── .reset        # optional reset timestamp (written by --reset)
└── .git/
    └── hooks/
        └── post-commit   # installed by chistory --install
```
