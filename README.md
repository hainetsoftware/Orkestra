# Orkestra

Orkestra is a local terminal app for coordinating coding agents. It runs approved tasks in separate Git worktrees and keeps task history, logs, and verification reports on your Mac.

## Install and run

Requires Python 3.11 or newer, Git, and the CLI for each agent you use.

```sh
git clone https://github.com/hainetsoftware/Orkestra.git
cd Orkestra
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/orkestra --repo /path/to/project
```

Use the primary Git checkout of the project. Start new agent tasks from a clean checkout. Orkestra keeps its state in the managed project's .loconv/ directory and can read existing LoConv tasks there. Ignore .loconv/ in the managed checkout. For a local-only exclusion, add .loconv/ to the checkout's .git/info/exclude.

## Tasks and agents

- **N** creates a task, **R** approves and runs it, **X** cancels, **D** shows Git status, **L** opens logs, **V** repeats verification when supported, and **Q** exits.
- Configure Codex, Antigravity (agy), OpenCode, and Cline in the managed project's .loconv/agents.json. Each command is an array of arguments with exactly one {prompt} placeholder. Authenticate the CLIs separately.
- Worktrees separate Git checkouts. Agent process permissions depend on the CLI and operating system. Review the configured commands before approving a run, especially the Cline auto-approval flag. Cloud agents can transmit prompts and source. Use synthetic fixtures and keep credentials and personal media out of agent inputs.
- Orkestra leaves commits, merges, and pushes to you.

## Verify LoConv

For successful LoConv tasks, the verifier runs automatically. Press **V** to repeat it without rerunning the agent. You can also invoke it against a LoConv checkout or task worktree:

```sh
.venv/bin/python -m orkestra.verify --worktree /path/to/LoConv --base main
```

The suite uses macOS process isolation and synthetic media. It checks the Tauri security configuration, Rust/Tauri, TypeScript, React, and the debug build. It uses cached Cargo and npm dependencies offline. Populate those caches with the project's locked dependencies before the first run. To require the FFmpeg tests, pass --audio and configure absolute executable paths and SHA-256 digests in the managed project's .loconv/verification-tools.json. Use the keys LOCONV_DEV_FFMPEG, LOCONV_DEV_FFMPEG_SHA256, LOCONV_DEV_FFPROBE, and LOCONV_DEV_FFPROBE_SHA256.

Reports stay under .loconv/verification/ in the managed checkout. The Checks column shows the test outcome separately from the task's execution status. A successful debug build and synthetic tests leave graphical acceptance and packaging to a separate check.

## Test Orkestra

```sh
.venv/bin/python -m pip install '.[dev]'
.venv/bin/python -m pytest -q
```
