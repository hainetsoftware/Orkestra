"""Textual terminal UI for local multi-agent development."""
from __future__ import annotations

import argparse
import asyncio
import subprocess
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Log, Select, Static, TextArea

from .core import AGENTS, Orchestrator, Task


class NewTask(ModalScreen[tuple[str, str, str] | None]):
    CSS = """
    NewTask { align: center middle; }
    #dialog { width: 86%; height: 75%; padding: 1 2; background: $panel; border: thick $primary; }
    #dialog TextArea { height: 1fr; }
    #dialog Input { margin-bottom: 1; }
    #dialog Horizontal { height: auto; margin-top: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("New agent task — nothing runs until you approve")
            yield Select([(a.upper(), a) for a in AGENTS], value="codex", id="agent")
            yield Input(placeholder="Task title", id="title")
            yield Label("Describe the implementation request (Ctrl+S to submit)")
            yield TextArea(id="prompt")
            with Horizontal():
                yield Button("Queue task", variant="success", id="queue")
                yield Button("Cancel", id="close")

    @on(Button.Pressed, "#close")
    def close(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#queue")
    def queue(self) -> None:
        agent = self.query_one("#agent", Select).value
        title = self.query_one("#title", Input).value.strip()
        prompt = self.query_one("#prompt", TextArea).text.strip()
        if not isinstance(agent, str) or not title or not prompt:
            self.notify("Agent, title, and prompt are required", severity="error")
            return
        self.dismiss((agent, title, prompt))


class ConfirmRun(ModalScreen[bool]):
    CSS = """
    ConfirmRun { align: center middle; }
    #confirmation { width: 75%; height: auto; padding: 2; background: $panel; border: thick $warning; }
    #confirmation Horizontal { height: auto; margin-top: 1; }
    """

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirmation"):
            yield Label(self.message)
            with Horizontal():
                yield Button("Approve", variant="warning", id="yes")
                yield Button("Back", id="no")

    @on(Button.Pressed, "#yes")
    def yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def no(self) -> None:
        self.dismiss(False)


class OrkestraApp(App):
    TITLE = "Orkestra | Agent Orchestrator"
    SUB_TITLE = "Human-approved multi-agent engineering"
    BINDINGS = [
        ("n", "new_task", "New task"),
        ("r", "run_task", "Run selected"),
        ("x", "stop_task", "Stop selected"),
        ("d", "show_diff", "Git status"),
        ("l", "load_log", "Read log"),
        ("v", "verify_task", "Verify"),
        ("q", "quit_safely", "Quit"),
    ]
    CSS = """
    Screen { background: $background; }
    #top { height: 3; background: $panel; content-align: center middle; }
    #main { height: 1fr; }
    #left { width: 54%; border: round $primary; }
    #right { width: 46%; border: round $primary; }
    #agent-status { height: 5; padding: 1; }
    DataTable { height: 1fr; }
    Log { height: 1fr; padding: 1; }
    #action-bar { height: 3; padding: 0 1; }
    #action-bar Button { margin-right: 1; }
    #details { height: 4; padding: 1; background: $panel; }
    """

    def __init__(self, repo: Path):
        super().__init__()
        self.repo = repo
        self.orchestrator = Orchestrator(repo, self.receive_line)
        self.current_log_id: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(f" REPOSITORY: {self.repo}", id="top")
        yield Static("Checking agents…", id="agent-status")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Label(" TASK QUEUE")
                yield DataTable(id="tasks", cursor_type="row")
                yield Static("Select a task to view details.", id="details")
            with Vertical(id="right"):
                yield Label(" LIVE OUTPUT / LOGS")
                yield Log(id="output", highlight=False, max_lines=2000)
        with Horizontal(id="action-bar"):
            yield Button("New [N]", id="new", variant="primary")
            yield Button("Run [R]", id="run", variant="success")
            yield Button("Stop [X]", id="stop", variant="error")
            yield Button("Status [D]", id="diff")
            yield Button("Log [L]", id="log")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#tasks", DataTable)
        table.add_columns("ID", "Agent", "Task", "Status", "Exit", "Checks")
        self.refresh_tasks()
        present = self.orchestrator.available()
        agents = "  ".join(f"{a.upper()}: {'READY' if present[a] else 'MISSING'}" for a in AGENTS)
        self.query_one("#agent-status", Static).update(f"AGENTS  {agents}\nRun only commands you trust. CLI access to internet is separate from the finished app's offline policy.")
        self.set_interval(1.5, self.refresh_tasks)

    def refresh_tasks(self) -> None:
        table = self.query_one("#tasks", DataTable)
        selected = self.selected_task()
        selected_id = selected.id if selected else None
        table.clear()
        row_idx = 0
        for i, task in enumerate(self.orchestrator.tasks):
            table.add_row(
                task.id, task.agent, task.title[:39], task.status,
                str(task.exit_code) if task.exit_code is not None else "—",
                task.verification or "—", key=task.id,
            )
            if task.id == selected_id:
                row_idx = i
        if self.orchestrator.tasks:
            table.move_cursor(row=row_idx)
        self.update_details()

    def selected_task(self) -> Task | None:
        table = self.query_one("#tasks", DataTable)
        if table.cursor_row < 0 or table.row_count == 0:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key((table.cursor_row, 0))
            return self.orchestrator.get(str(row_key.value))
        except (IndexError, KeyError):
            return None

    @on(DataTable.RowHighlighted, "#tasks")
    def update_details(self, _event=None) -> None:
        task = self.selected_task()
        if task:
            self.query_one("#details", Static).update(f"{task.id} | {task.agent} | {task.status}\nBranch: {task.branch}\n{task.note or task.title}")
        else:
            self.query_one("#details", Static).update("Select a task to view details.")

    def receive_line(self, task_id: str, line: str) -> None:
        if not line:
            return
        # Called by async worker on Textual's event loop.
        if self.is_mounted:
            self.query_one("#output", Log).write_line(f"[{task_id}] {line}")

    def action_new_task(self) -> None:
        self.push_screen(NewTask(), self._task_created)

    def _task_created(self, info: tuple[str, str, str] | None) -> None:
        if info:
            self.orchestrator.create(*info)
            self.refresh_tasks()

    def action_run_task(self) -> None:
        task = self.selected_task()
        if not task:
            self.notify("Select a task first", severity="warning")
            return
        if task.status not in ("queued", "failed", "interrupted"):
            self.notify("Task is not runnable", severity="warning")
            return
        self.push_screen(ConfirmRun(
            f"Execute {task.agent.upper()} for task '{task.title}'?\n\n"
            "This CLI agent can modify files and run commands inside its Git worktree. "
            "A worktree does NOT sandbox filesystem, network, or credentials. "
            "Review CLI permissions and never use personal media as samples. "
            "A successful run starts the local synthetic checks."
        ), lambda approved: self.orchestrator.launch(task) if approved else None)

    def action_verify_task(self) -> None:
        task = self.selected_task()
        if not task or task.status not in ("completed", "failed", "interrupted"):
            self.notify("Select a finished task first", severity="warning")
            return
        self.push_screen(ConfirmRun(
            "Run local checks in this task's worktree with synthetic fixtures?"
        ), lambda approved: self.orchestrator.launch_verification(task) if approved else None)

    async def action_stop_task(self) -> None:
        task = self.selected_task()
        if task:
            await self.orchestrator.cancel(task)
            self.refresh_tasks()

    def action_show_diff(self) -> None:
        task = self.selected_task()
        if task:
            self.receive_line(task.id, "WORKTREE GIT STATUS:\n" + self.orchestrator.diffstat(task))

    def action_load_log(self) -> None:
        task = self.selected_task()
        if not task:
            return
        path = self.orchestrator.logs / f"{task.id}.log"
        log = self.query_one("#output", Log)
        log.clear()
        if path.exists():
            for line in path.read_text(errors="replace").splitlines()[-400:]:
                log.write_line(line)
        else:
            log.write_line("No log recorded yet.")

    async def action_quit_safely(self) -> None:
        active = [t for t in self.orchestrator.tasks if t.status == "running" or t.verification == "checking"]
        if active:
            self.push_screen(ConfirmRun("Stop all running agents and checks and exit?"), self._quit_decision)
        else:
            self.exit()

    def _quit_decision(self, approved: bool) -> None:
        if approved:
            asyncio.create_task(self._shutdown_and_exit())

    async def _shutdown_and_exit(self) -> None:
        await self.orchestrator.shutdown()
        self.exit()

    @on(Button.Pressed, "#new")
    def new_pressed(self) -> None:
        self.action_new_task()

    @on(Button.Pressed, "#run")
    def run_pressed(self) -> None:
        self.action_run_task()

    @on(Button.Pressed, "#stop")
    async def stop_pressed(self) -> None:
        await self.action_stop_task()

    @on(Button.Pressed, "#diff")
    def diff_pressed(self) -> None:
        self.action_show_diff()

    @on(Button.Pressed, "#log")
    def log_pressed(self) -> None:
        self.action_load_log()


def main() -> None:
    parser = argparse.ArgumentParser(description="Orkestra multi-agent development TUI")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Local Git checkout (default: current directory)")
    args = parser.parse_args()
    try:
        OrkestraApp(args.repo).run()
    except (ValueError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
