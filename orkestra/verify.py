"""Local, synthetic-only sprint verification and Git review evidence."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ROOT_FILES = {"Cargo.toml", "Cargo.lock"}
SOURCE_FILES = {
    "crates": {".rs", ".toml"},
    "contracts/fixtures": {".json"},
    "apps/desktop": {".cjs", ".css", ".html", ".js", ".json", ".json5", ".mjs", ".rs", ".toml", ".ts", ".tsx"},
}
ICON = "apps/desktop/src-tauri/icons/icon.png"
CAPABILITIES = {"allow-get-api-version", "allow-ipc-v1", "core:event:allow-listen", "core:event:allow-unlisten"}
CSP_SOURCES = {
    "default-src": {"'self'", "'none'"},
    "connect-src": {"ipc:", "http://ipc.localhost", "'none'"},
    "style-src": {"'self'", "'unsafe-inline'", "'none'"},
    "script-src": {"'self'", "'none'"},
    "img-src": {"'self'", "data:", "'none'"},
}
DEV_TOOLS = ("FFMPEG", "FFPROBE")
TEST_TOTAL = re.compile(rb"test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored;")
TIMEOUT = 1200


class CheckError(Exception):
    """An error code that is safe to put in a report."""


def is_converter_workspace(repo: Path) -> bool:
    return (repo / "Cargo.toml").is_file() and (repo / "apps/desktop/src-tauri/tauri.conf.json").is_file()


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.fsmonitor=false", "--no-pager", *args],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    if result.returncode:
        raise CheckError("git_command_failed")
    return result.stdout


def commit(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").strip().decode("ascii")


def git_state(repo: Path, base_repo: Path, base_ref: str | None) -> dict:
    porcelain = git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    state = {"head": commit(repo, "HEAD"), "clean": not porcelain}
    if base_ref:
        state["base"] = commit(base_repo, base_ref)
        state["base_clean"] = not git(base_repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        state["fast_forward"] = git(repo, "merge-base", state["head"], state["base"]).strip().decode() == state["base"]
    return state


def regular_file(root: Path, relative: str) -> Path:
    path = root / relative
    if path.resolve() != path or not path.is_file():
        raise CheckError("source_not_regular")
    return path


def source_snapshot(repo: Path, dest: Path | None = None) -> str:
    """Copy only source inputs from the index and visible untracked files."""
    entries = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    digest = hashlib.sha256()
    for name in sorted(set(entries.split(b"\0")) - {b""}):
        relative = os.fsdecode(name)
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise CheckError("unsafe_source_path")
        if relative == ICON:
            # The sole binary build input is the committed application icon.
            data = git(repo, "cat-file", "blob", f"HEAD:{ICON}")
        elif relative in ROOT_FILES or any(
            path.is_relative_to(folder) and path.suffix in extensions
            for folder, extensions in SOURCE_FILES.items()
        ):
            source = repo / path
            if not source.exists() and not source.is_symlink():
                continue  # A tracked deletion is intentionally absent.
            data = regular_file(repo, relative).read_bytes()
            if b"\0" in data:
                raise CheckError("binary_source_rejected")
        else:
            continue
        digest.update(name + b"\0" + hashlib.sha256(data).digest())
        if dest:
            target = dest / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    return digest.hexdigest()


def check_policy(snapshot: Path) -> None:
    native = snapshot / "apps/desktop/src-tauri"
    try:
        config = json.loads((native / "tauri.conf.json").read_text())
        app = config["app"]
        security = app["security"]
        csp = security["csp"]
        directives = {}
        for directive in csp.split(";"):
            if words := directive.split():
                key, *values = words
                if key in directives or not values or not set(values) <= CSP_SOURCES.get(key, set()):
                    raise CheckError("production_csp_changed")
                directives[key] = set(values)
        if not {"default-src", "connect-src"} <= directives.keys():
            raise CheckError("production_csp_changed")
        if app.get("withGlobalTauri") is not False or security.get("dangerousDisableAssetCspModification"):
            raise CheckError("production_csp_changed")
        if security.get("assetProtocol", {}).get("enable") or security.get("capabilities") != ["main-capability"]:
            raise CheckError("native_capabilities_changed")
        capability = json.loads((native / "capabilities/default.json").read_text())
        if (
            set(capability) - {"$schema", "identifier", "description", "windows", "permissions"}
            or capability["identifier"] != "main-capability"
            or capability["windows"] != ["main"]
            or set(capability["permissions"]) != CAPABILITIES
        ):
            raise CheckError("native_capabilities_changed")
        overrides = ("tauri.conf.json5", "tauri.macos.conf.json", "tauri.macos.conf.json5")
        if any((native / name).exists() for name in overrides) or next(native.glob("Tauri*.toml"), None):
            raise CheckError("native_config_override")
    except (OSError, KeyError, TypeError, AttributeError, ValueError):
        raise CheckError("invalid_native_configuration") from None


def tool_pins(config: Path) -> dict[str, str]:
    pins = os.environ
    if config.exists():
        try:
            pins = json.loads(regular_file(config.parent, config.name).read_text())
            if not isinstance(pins, dict):
                raise CheckError("invalid_audio_pin_config")
        except (ValueError, OSError):
            raise CheckError("invalid_audio_pin_config") from None
    env = {}
    for name in DEV_TOOLS:
        key = f"LOCONV_DEV_{name}"
        raw = pins.get(key, "")
        expected = pins.get(f"{key}_SHA256", "")
        if not isinstance(raw, str) or not isinstance(expected, str):
            raise CheckError("invalid_audio_pin_config")
        executable = Path(raw)
        if not executable.is_absolute() or not re.fullmatch(r"[a-fA-F0-9]{64}", expected):
            raise CheckError("audio_pins_missing")
        executable = executable.resolve()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise CheckError("audio_tool_unavailable")
        with executable.open("rb") as binary:
            if hashlib.file_digest(binary, "sha256").hexdigest() != expected.lower():
                raise CheckError("audio_pin_mismatch")
        env[key] = str(executable)
        env[f"{key}_SHA256"] = expected.lower()
    return {"LOCONV_ENABLE_DEV_TOOLS": "1", **env}


def sandbox_profile(run_dir: Path, cache: Path, pins: dict[str, str]) -> str:
    home = Path.home()
    read_paths = [
        "/System", "/Library/Developer", "/Library/Apple", "/usr", "/bin", "/sbin", "/opt/homebrew",
        "/private/etc", "/private/var/db", "/dev", str(run_dir), str(cache),
        str(home / ".cargo" / "bin"), str(home / ".cargo" / "registry"),
        str(home / ".rustup"), str(home / ".npm" / "_cacache"), str(Path(sys.prefix)),
        *(pins[key] for key in (f"LOCONV_DEV_{name}" for name in DEV_TOOLS) if key in pins),
    ]
    write_paths = [str(run_dir), str(cache), str(home / ".npm" / "_cacache")]
    reads = " ".join(f"(subpath {json.dumps(path)})" for path in read_paths)
    writes = " ".join(f"(subpath {json.dumps(path)})" for path in write_paths)
    # Cargo locks its dependency cache even in offline mode.
    cargo = home / ".cargo"
    cargo_locks = " ".join(
        f"(literal {json.dumps(str(cargo / name))})"
        for name in (".package-cache", ".package-cache-mutate", ".global-cache", ".global-cache-journal")
    )
    return (
        "(version 1) (allow default) (deny network*) "
        '(deny file-read-data (subpath "/Users") (subpath "/Volumes") '
        '(subpath "/private/var/folders") (subpath "/private/tmp")) (deny file-write*) '
        f"(allow file-read-data {reads} {cargo_locks}) "
        f'(allow file-write* {writes} {cargo_locks} (literal "/dev/null") (literal "/dev/tty"))'
    )


def run_check(argv: list[str], cwd: Path, env: dict[str, str], profile: str) -> dict:
    start = time.monotonic()
    executable = shutil.which(argv[0], path=env["PATH"])
    if not executable:
        return {"status": "blocked", "reason": "tool_missing"}
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(
            ["/usr/bin/sandbox-exec", "-p", profile, executable, *argv[1:]], cwd=cwd, env=env,
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            code = proc.wait(timeout=TIMEOUT)
        except BaseException as exc:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            if isinstance(exc, subprocess.TimeoutExpired):
                return {"status": "failed", "reason": "timeout"}
            raise
        output.seek(0)
        counts = [0, 0, 0]
        while chunk := output.readline(4096):
            if match := TEST_TOTAL.match(chunk):
                for index, count in enumerate(match.groups()):
                    counts[index] += int(count)
    result = {"status": "passed" if code == 0 else "failed", "seconds": round(time.monotonic() - start, 1)}
    if code:
        result["exit"] = code
    if any(counts):
        result["tests"] = dict(zip(("passed", "failed", "ignored"), counts))
    return result


def verify(
    repo: Path,
    main: Path,
    report_dir: Path,
    base: str | None,
    audio: bool,
    checkpoint: Callable[[dict], None],
) -> dict:
    report = {
        "schema": 1, "started_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "outcome": "blocked", "git_review": "blocked", "checks": {},
    }
    checks = report["checks"]
    try:
        state = git_state(repo, main, base)
        report["git"] = state
        if sys.platform != "darwin":
            raise CheckError("macos_sandbox_required")
        cache = report_dir / "cargo-target"
        if cache.resolve() != cache:
            raise CheckError("unsafe_build_cache")
        cache.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="loconv-verify-") as temporary:
            run_dir = Path(temporary).resolve()
            snapshot = run_dir / "source"
            snapshot.mkdir()
            report["source_sha256"] = source_snapshot(repo, snapshot)
            if not is_converter_workspace(snapshot):
                raise CheckError("converter_workspace_missing")
            try:
                check_policy(snapshot)
                checks["privacy_config"] = {"status": "passed"}
            except CheckError as exc:
                checks["privacy_config"] = {"status": "failed", "reason": str(exc)}
                raise

            pins = {}
            pin_config = main / ".loconv" / "verification-tools.json"
            audio = audio or pin_config.exists()
            if audio:
                try:
                    pins = tool_pins(pin_config)
                except CheckError as exc:
                    checks["real_audio"] = {"status": "blocked", "reason": str(exc)}
            else:
                checks["real_audio"] = {"status": "skipped", "reason": "explicit_pins_required"}
            desktop = snapshot / "apps/desktop"
            for folder in ("home", "tmp"):
                (run_dir / folder).mkdir()
            home = Path.home()
            env = {
                "PATH": os.pathsep.join((str(home / ".cargo/bin"), "/opt/homebrew/bin", "/usr/bin", "/bin")),
                "HOME": str(run_dir / "home"),
                "TMPDIR": str(run_dir / "tmp"),
                "CARGO_HOME": str(home / ".cargo"),
                "RUSTUP_HOME": str(home / ".rustup"),
                "CARGO_TARGET_DIR": str(cache),
                "CARGO_NET_OFFLINE": "true",
                "PYTHONDONTWRITEBYTECODE": "1",
                "npm_config_cache": str(home / ".npm"),
                "npm_config_offline": "true",
                "CI": "1",
                "LANG": "en_US.UTF-8",
            }
            profile = sandbox_profile(run_dir, cache.resolve(), pins)

            def check(name: str, argv: list[str], cwd: Path = snapshot, extra: dict | None = None) -> bool:
                checks[name] = {"status": "checking"}
                checkpoint({**report, "outcome": "checking"})
                checks[name] = run_check(argv, cwd, {**env, **(extra or {})}, profile)
                return checks[name]["status"] == "passed"

            with tempfile.TemporaryDirectory(prefix="loconv-boundary-") as outside:
                marker = Path(outside) / "synthetic.txt"
                marker.write_text("synthetic")
                probe = (
                    "import pathlib, socket, sys\n"
                    "try:\n pathlib.Path(sys.argv[1]).read_text()\n"
                    "except PermissionError: pass\n"
                    "else: sys.exit(1)\n"
                    "try:\n socket.create_connection(('127.0.0.1', 9), timeout=1).close()\n"
                    "except PermissionError: pass\n"
                    "else: sys.exit(2)\n"
                )
                if not check("macos_isolation", [sys.executable, "-c", probe, str(marker)]):
                    raise CheckError("macos_sandbox_unavailable")

            whitespace = subprocess.run(
                ["git", "-C", str(repo), "-c", "core.fsmonitor=false", "diff", "--no-ext-diff", "--no-textconv", "--check",
                 state.get("base", "HEAD"), "--", *ROOT_FILES, *SOURCE_FILES],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
            checks["git_diff"] = {"status": "passed" if whitespace.returncode == 0 else "failed"}

            if check("frontend_dependencies", ["npm", "ci", "--offline", "--ignore-scripts", "--no-audit", "--no-fund"], desktop):
                check("typescript", ["npm", "--ignore-scripts", "run", "typecheck"], desktop)
                check("react", ["npm", "--ignore-scripts", "test"], desktop)
                check("frontend_build", ["npm", "--ignore-scripts", "run", "build"], desktop)
            check("rust_format", ["cargo", "fmt", "--all", "--", "--check"])
            cargo_tests = ["cargo", "test", "--workspace", "--locked", "--offline"]
            check("rust_tauri", cargo_tests)
            check("clippy", ["cargo", "clippy", "--workspace", "--locked", "--offline", "--all-targets", "--", "-D", "warnings"])
            if checks.get("frontend_build", {}).get("status") == "passed":
                check("macos_binary", ["cargo", "build", "--locked", "--offline", "-p", "loconv-desktop", "--features", "custom-protocol"])
            real_tests = [*cargo_tests, "--features", "dev-tools"]
            if pins and check("real_audio", [*real_tests, "real_", "--", "--ignored", "--skip", "real_video"], extra=pins):
                expected = 4 if (snapshot / "crates/engine/src/backends/ffmpeg_audio.rs").exists() else 2
                if checks["real_audio"].get("tests", {}).get("passed", 0) < expected:
                    checks["real_audio"].update(status="failed", reason="real_tests_missing")
            encoder_source = snapshot / "crates/engine/src/backends/ffmpeg_audio.rs"
            if encoder_source.is_file() and "fn real_video_matrix(" in encoder_source.read_text():
                if not pins:
                    checks["real_video"] = {"status": "skipped", "reason": "explicit_pins_required"}
                elif check("real_video", [*real_tests, "real_video", "--", "--ignored"], extra=pins):
                    if not checks["real_video"].get("tests", {}).get("passed", 0):
                        checks["real_video"].update(status="failed", reason="real_tests_missing")


            if git_state(repo, main, base) != state or source_snapshot(repo) != report["source_sha256"]:
                raise CheckError("source_or_base_changed")
            values = {item["status"] for item in checks.values()}
            report["outcome"] = "failed" if "failed" in values else "blocked" if "blocked" in values else "passed"
            if report["outcome"] == "passed" and base:
                if not state["clean"] or not state["base_clean"]:
                    report["git_review"] = "pending_clean_commits"
                elif not state["fast_forward"]:
                    report["git_review"] = "pending_integration_candidate"
                elif state["head"] == state["base"]:
                    report["git_review"] = "already_at_base"
                elif not audio:
                    report["git_review"] = "pending_real_audio"
                else:
                    report["git_review"] = "pending_human_approval"
    except CheckError as exc:
        report["reason"] = str(exc)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        report["reason"] = "local_io_or_tool_error"
    return report


def write_report(path: Path, report: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        json.dump(report, output, indent=2, sort_keys=True)
        output.write("\n")
    os.replace(output.name, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", type=Path, default=Path.cwd())
    parser.add_argument("--base", help="Local integration ref to review")
    parser.add_argument("--audio", action="store_true", help="Require pinned synthetic FFmpeg/FFprobe tests")
    args = parser.parse_args()
    try:
        repo = Path(os.fsdecode(git(args.worktree, "rev-parse", "--show-toplevel").rstrip(b"\n"))).resolve()
        common = Path(os.fsdecode(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").rstrip(b"\n"))).resolve()
        main_repo = common.parent
        report_dir = main_repo / ".loconv" / "verification"
        if report_dir.resolve() != report_dir:
            raise CheckError("unsafe_report_directory")
        report_dir.mkdir(parents=True, exist_ok=True)
        report_key = "checkout" if main_repo == repo else hashlib.sha256(os.fsencode(repo)).hexdigest()[:12]
        if repo.parent == main_repo / ".loconv" / "worktrees" and re.fullmatch(r"[0-9a-f]{8}", repo.name):
            report_key = repo.name
        destination = report_dir / f"{report_key}.json"
        lock_path = report_dir / f"{report_key}.lock"
        with os.fdopen(os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), "wb") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CheckError("verification_already_running") from None
            write_report(destination, {"schema": 1, "outcome": "checking"})
            try:
                report = verify(
                    repo, main_repo, report_dir, args.base, args.audio,
                    lambda state: write_report(destination, state),
                )
            except KeyboardInterrupt:
                write_report(destination, {"schema": 1, "outcome": "interrupted"})
                raise
            write_report(destination, report)
        print(json.dumps(report, sort_keys=True))
        return 0 if report["outcome"] == "passed" else 1
    except (CheckError, OSError) as exc:
        reason = str(exc) if isinstance(exc, CheckError) else "git_or_report_unavailable"
        print(json.dumps({"outcome": "blocked", "reason": reason}))
        return 1


def interrupt(_signum, _frame) -> None:
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupt)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
