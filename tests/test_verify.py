import json
import subprocess

import pytest

from orkestra.verify import CheckError, check_policy, source_snapshot


def test_snapshot_excludes_media_and_includes_untracked_source(tmp_path):
    repo, snapshot = tmp_path / "repo", tmp_path / "snapshot"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    source = repo / "crates" / "candidate.rs"
    source.parent.mkdir()
    source.write_text("fn synthetic() {}\n")
    (repo / ".gitignore").write_text("tests/generated/\n")
    generated = repo / "tests" / "generated"
    generated.mkdir(parents=True)
    (generated / "synthetic.wav").write_bytes(b"RIFF\0\0")
    (generated / "ignored.py").write_text("synthetic")
    (source.parent / "synthetic.png").write_bytes(b"\x89PNG")
    digest = source_snapshot(repo.resolve(), snapshot)
    assert {path.relative_to(snapshot).as_posix() for path in snapshot.rglob("*") if path.is_file()} == {
        "crates/candidate.rs"
    }
    assert source_snapshot(repo.resolve()) == digest
    source.write_text("fn changed() {}\n")
    assert source_snapshot(repo.resolve()) != digest
    source.unlink()
    source.symlink_to(generated / "ignored.py")
    with pytest.raises(CheckError, match="source_not_regular"):
        source_snapshot(repo.resolve())


def test_release_security_rejects_additional_network_sources(tmp_path):
    native = tmp_path / "apps" / "desktop" / "src-tauri"
    (native / "capabilities").mkdir(parents=True)
    capability = {
        "identifier": "main-capability",
        "windows": ["main"],
        "permissions": ["allow-get-api-version", "allow-ipc-v1", "core:event:allow-listen", "core:event:allow-unlisten"],
    }
    (native / "capabilities" / "default.json").write_text(json.dumps(capability))
    config = {"app": {"withGlobalTauri": False, "security": {
        "capabilities": ["main-capability"],
        "csp": "default-src 'self'; connect-src ipc: http://ipc.localhost",
    }}}
    config_path = native / "tauri.conf.json"
    config_path.write_text(json.dumps(config))
    check_policy(tmp_path)
    config["app"]["security"]["csp"] += " https://example.invalid"
    config_path.write_text(json.dumps(config))
    with pytest.raises(CheckError, match="production_csp_changed"):
        check_policy(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("converter", [True, False])
async def test_successful_task_starts_verification_only_for_converter(tmp_path, monkeypatch, converter):
    from orkestra.core import Orchestrator

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "Cargo.toml").write_text("[workspace]\n")
    if converter:
        config = repo / "apps/desktop/src-tauri/tauri.conf.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.org",
         "commit", "-qm", "Initialize synthetic workspace"],
        check=True,
    )
    orchestrator = Orchestrator(repo, lambda *_: None)
    orchestrator.config["codex"] = ["python3", "-c", "pass", "{prompt}"]
    task = orchestrator.create("codex", "Synthetic task", "Run a test")
    verified = []

    async def record(candidate):
        assert candidate.status == "completed"
        verified.append(candidate.id)

    monkeypatch.setattr(orchestrator, "verify", record)
    await orchestrator.execute(task)
    assert verified == ([task.id] if converter else [])
