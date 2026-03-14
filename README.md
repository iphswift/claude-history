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

### 2. Install the hook into a target project

Run the following from inside the git project you want to track:

```bash
python3 /path/to/claude-history/main.py --install
```

This writes `.git/hooks/post-commit` in the target project and makes it executable. The hook calls `main.py` on every commit.

To install the hook for the `claude-history` repo itself:

```bash
cd /path/to/claude-history
python3 main.py --install
```

### 3. Verify

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
python3 main.py
```

This writes `conversation.md` in the current directory.

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
│   └── def67890.md   # conversation for commit def67890
└── .git/
    └── hooks/
        └── post-commit   # installed by main.py --install
```
