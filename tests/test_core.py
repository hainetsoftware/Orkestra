import json
import subprocess
from pathlib import Path

import pytest

from orkestra.core import AGENTS, Orchestrator, safe_branch


def test_branches():
    assert safe_branch('1234abcd', 'codex') == 'agent/codex/1234abcd'
    with pytest.raises(ValueError):
        safe_branch('../oops', 'agy')
    with pytest.raises(ValueError):
        safe_branch('1234abcd', 'evil')


def temp_repo(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-b', 'main', str(repo)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.org', 'commit', '--allow-empty', '-m', 'init'], check=True, capture_output=True)
    return repo


def test_task_persistence(tmp_path):
    repo = temp_repo(tmp_path)
    engine = Orchestrator(repo, lambda *_: None)
    task = engine.create('codex', 'Build rust core', 'Create a minimal backend')
    assert task.status == 'queued'
    assert task.branch.startswith('agent/codex/')
    assert '{prompt}' in json.loads(engine.config_path.read_text())['codex'][-1]
    reloaded = Orchestrator(repo, lambda *_: None)
    assert reloaded.get(task.id).title == 'Build rust core'
    assert all(a in reloaded.available() for a in AGENTS)


def test_command_avoids_shell_injection(tmp_path):
    repo = temp_repo(tmp_path)
    engine = Orchestrator(repo, lambda *_: None)
    task = engine.create('agy', 'Test', 'Use filename $(touch /tmp/pwned)')
    argv = engine.command(task)
    assert argv[0] == 'agy'
    assert any('$(touch /tmp/pwned)' in item for item in argv)
    assert isinstance(argv, list)


@pytest.mark.asyncio
async def test_dummy_agent(tmp_path):
    repo = temp_repo(tmp_path)
    messages = []
    engine = Orchestrator(repo, lambda *args: messages.append(args))
    engine.config['codex'] = ['python3', '-c', 'print("WORKS")', '{prompt}']
    task = engine.create('codex', 'Demo', 'Hello')
    await engine.execute(task)
    assert task.status == 'completed'
    assert task.exit_code == 0
    assert 'WORKS' in (engine.logs / f'{task.id}.log').read_text()
    assert Path(task.worktree).exists()
