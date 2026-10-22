"""Isolated lifecycle for the real Go app and two owned loopback sidecars."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
RUN_ROOT = REPO_ROOT / ".agents" / "run-data"
EVIDENCE_ROOT = REPO_ROOT / ".agents" / "evidence" / "runs"
SOURCE_ROOT = REPO_ROOT / "research" / "semantic-sharing" / "src"
_OWNED: dict[int, subprocess.Popen] = {}


class LifecycleError(RuntimeError):
    pass


def new_run_id() -> str:
    return uuid.uuid4().hex


def start(run_id: str, ready_timeout: float = 45.0) -> dict:
    _validate_run_id(run_id)
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    run_dir = RUN_ROOT / run_id
    if run_dir.exists():
        raise LifecycleError("run directory already exists; refusing to reuse it")
    evidence_dir = EVIDENCE_ROOT / run_id
    if evidence_dir.exists() and any(evidence_dir.iterdir()):
        raise LifecycleError("evidence directory already exists; refusing to reuse the run ID")
    run_dir.mkdir()
    (run_dir / ".semshare-owned").write_text(run_id, encoding="ascii")
    binary = run_dir / ("secushare-server.exe" if os.name == "nt" else "secushare-server")
    app_log = run_dir / "app.log"
    sidecar_logs = [run_dir / "storage-a.log", run_dir / "storage-b.log"]
    state = {
        "schema": 1,
        "run_id": run_id,
        "run_dir": str(run_dir.resolve()),
        "app_url": "",
        "api_url": "",
        "binary": str(binary.resolve()),
        "storage_urls": [],
        "storage_data_dirs": [str((run_dir / "storage-a").resolve()), str((run_dir / "storage-b").resolve())],
        "storage_instance_ids": [run_id + "-a", run_id + "-b"],
        "pids": {},
        "logs": [str(app_log), *(str(item) for item in sidecar_logs)],
    }
    _write_state(run_dir, state)
    processes: list[subprocess.Popen] = []
    try:
        go_exe = os.environ.get("GO_EXECUTABLE") or shutil.which("go")
        if not go_exe:
            raise LifecycleError("Go is unavailable; native app startup needs a Go toolchain")
        env = os.environ.copy()
        env["GOTOOLCHAIN"] = "local"
        env["GOCACHE"] = str(run_dir / "go-cache")
        env["GOMODCACHE"] = str(run_dir / "go-mod-cache")
        env["GOPATH"] = str(run_dir / "go-path")
        env["GOFLAGS"] = (env.get("GOFLAGS", "") + " -modcacherw").strip()
        with (run_dir / "build.log").open("wb") as output:
            built = subprocess.run(
                [go_exe, "build", "-o", str(binary), "./cmd/server"],
                cwd=REPO_ROOT / "backend",
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=600,
            )
        if built.returncode:
            tail = (run_dir / "build.log").read_text(errors="replace")[-4000:]
            raise LifecycleError(f"native app build failed ({built.returncode}): {tail}")

        ports: list[int] = []
        while len(ports) < 3:
            candidate = _free_port()
            if candidate not in ports:
                ports.append(candidate)
        app_port, first_port, second_port = ports
        state["app_url"] = f"http://127.0.0.1:{app_port}"
        state["api_url"] = state["app_url"] + "/api/v1"
        state["storage_urls"] = [f"http://127.0.0.1:{first_port}", f"http://127.0.0.1:{second_port}"]
        _write_state(run_dir, state)

        app_env = env.copy()
        app_env.update({
            "SERVER_BIND_ADDRESS": "127.0.0.1",
            "SERVER_PORT": str(app_port),
            "ALLOW_ORIGINS": "http://127.0.0.1:5173",
            "DATABASE_PATH": str(run_dir / "secushare.db"),
            "STORAGE_PATH": str(run_dir / "files"),
            "METRICS_ENABLED": "false",
            "LOG_FORMAT": "json",
            "LOG_LEVEL": "info",
        })
        processes.append(_spawn([str(binary)], run_dir, app_env, app_log))
        state["pids"]["app"] = processes[-1].pid
        _write_state(run_dir, state)

        module_env = os.environ.copy()
        module_env["PYTHONPATH"] = str(SOURCE_ROOT) + (os.pathsep + module_env["PYTHONPATH"] if module_env.get("PYTHONPATH") else "")
        for name, port, data_dir, instance_id, log_path in zip(
            ("storage_a", "storage_b"),
            (first_port, second_port),
            state["storage_data_dirs"],
            state["storage_instance_ids"],
            sidecar_logs,
            strict=True,
        ):
            processes.append(_spawn(
                [sys.executable, "-m", "semshare.sidecar", "--host", "127.0.0.1", "--port", str(port), "--data-dir", data_dir, "--instance-id", instance_id],
                REPO_ROOT,
                module_env,
                log_path,
            ))
            state["pids"][name] = processes[-1].pid
            _write_state(run_dir, state)

        _wait_health(state, ready_timeout)
        state["started_at_epoch"] = time.time()
        _write_state(run_dir, state)
        return state
    except Exception as launch_error:
        stop_errors = []
        for process in reversed(processes):
            if process.poll() is not None:
                continue
            try:
                process.terminate()
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=2)
                except Exception as exc:
                    stop_errors.append(exc)
            except Exception as exc:
                stop_errors.append(exc)
        try:
            cleanup(run_id)
        except Exception as cleanup_error:
            stop_errors.append(cleanup_error)
        if stop_errors:
            raise ExceptionGroup("launch failed and owned-process cleanup failed", [launch_error, *stop_errors]) from launch_error
        raise


def doctor(run_id: str) -> dict:
    state = load_state(run_id)
    for role, pid in state["pids"].items():
        if not _is_owned(int(pid), role, state):
            raise LifecycleError(f"owned process is not running: {role}")
    _check_app(state)
    from .oram import Sidecar

    for url, instance_id in zip(state["storage_urls"], state["storage_instance_ids"], strict=True):
        Sidecar(url, instance_id=instance_id).health()
    return {"run_id": run_id, "status": "ready", "app": state["app_url"], "storage_sidecars": len(state["storage_urls"])}


def load_state(run_id: str) -> dict:
    _validate_run_id(run_id)
    run_dir = (RUN_ROOT / run_id).resolve()
    if run_dir.parent != RUN_ROOT.resolve() or not (run_dir / ".semshare-owned").is_file():
        raise LifecycleError("run ownership marker is missing or outside the isolated run root")
    if (run_dir / ".semshare-owned").read_text(encoding="ascii") != run_id:
        raise LifecycleError("run ownership marker does not match the requested run ID")
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    if state.get("schema") != 1 or state.get("run_id") != run_id:
        raise LifecycleError("run state has an invalid schema or identity")
    return state


def cleanup(run_id: str, timeout: float = 8.0) -> dict:
    state = load_state(run_id)
    stopped = []
    for role, raw_pid in reversed(list(state["pids"].items())):
        pid = int(raw_pid)
        process = _OWNED.get(pid)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                if not _is_owned(pid, role, state):
                    raise LifecycleError(f"process identity changed before forced stop: {role}")
                process.kill()
                process.wait(timeout=2)
            stopped.append(role)
            continue
        if not _process_exists(pid):
            continue
        if not _is_owned(pid, role, state):
            raise LifecycleError(f"refusing to stop PID {pid}; it no longer matches owned process {role}")
        _signal_process(pid)
        if not _wait_exit(pid, timeout):
            if not _is_owned(pid, role, state):
                raise LifecycleError(f"process identity changed before forced stop: {role}")
            _force_stop(pid)
            if not _wait_exit(pid, 2):
                raise LifecycleError(f"owned process did not exit: {role}")
        stopped.append(role)

    for role, raw_pid in state["pids"].items():
        if _process_exists(int(raw_pid)) and _is_owned(int(raw_pid), role, state):
            raise LifecycleError(f"owned process is still running after cleanup: {role}")
    evidence_dir = EVIDENCE_ROOT / run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    for raw_log in state["logs"]:
        source = Path(raw_log)
        if source.is_file():
            shutil.copyfile(source, evidence_dir / source.name)
    run_dir = Path(state["run_dir"]).resolve()
    if run_dir.parent != RUN_ROOT.resolve() or run_dir.name != run_id:
        raise LifecycleError("run directory failed the cleanup boundary check")
    shutil.rmtree(run_dir)
    return {"run_id": run_id, "stopped": stopped, "run_data_removed": True}


def _spawn(command: list[str], cwd: Path, env: dict, log_path: Path) -> subprocess.Popen:
    stream = log_path.open("ab")
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=(os.name != "nt"),
        )
    finally:
        stream.close()
    _OWNED[process.pid] = process
    return process


def _write_state(run_dir: Path, state: dict) -> None:
    temp = run_dir / "run.pending"
    temp.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    os.replace(temp, run_dir / "run.json")


def _wait_health(state: dict, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error = "not ready"
    while time.monotonic() < deadline:
        if any(not _process_exists(int(pid)) for pid in state["pids"].values()):
            raise LifecycleError("an owned service exited before becoming ready")
        try:
            _check_app(state)
            doctor_sidecars = []
            from .oram import Sidecar

            doctor_sidecars = [Sidecar(url, instance_id=identity) for url, identity in zip(state["storage_urls"], state["storage_instance_ids"], strict=True)]
            for sidecar in doctor_sidecars:
                sidecar.health()
            return
        except Exception as exc:
            last_error = str(exc)
            time.sleep(0.05)
    raise LifecycleError(f"owned app and sidecars did not become ready: {last_error}")


def _check_app(state: dict) -> None:
    try:
        with urllib.request.urlopen(state["app_url"] + "/health/ready", timeout=1) as response:
            result = json.loads(response.read(4096))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise LifecycleError("SecuShare readiness endpoint is unavailable") from exc
    if result.get("status") != "ok":
        raise LifecycleError(f"SecuShare readiness checks are not healthy: {result!r}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _is_owned(pid: int, role: str, state: dict) -> bool:
    if pid in _OWNED:
        process = _OWNED[pid]
        return process.poll() is None
    if not _process_exists(pid):
        return False
    if os.name != "nt":
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            executable = Path(f"/proc/{pid}/exe").resolve()
        except OSError:
            return False
        if role == "app":
            return executable == Path(state["binary"]).resolve()
        sidecar = "a" if role == "storage_a" else "b"
        return state["storage_instance_ids"][0 if sidecar == "a" else 1] in command and state["storage_data_dirs"][0 if sidecar == "a" else 1] in command
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", f"Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}' | Select-Object -ExpandProperty CommandLine"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    command = result.stdout
    if role == "app":
        return str(Path(state["binary"]).resolve()).lower() in command.lower()
    index = 0 if role == "storage_a" else 1
    return state["storage_instance_ids"][index] in command and state["storage_data_dirs"][index] in command


def _signal_process(pid: int) -> None:
    if os.name == "nt":
        result = subprocess.run(["taskkill.exe", "/PID", str(pid), "/T"], check=False, capture_output=True, timeout=5)
        if result.returncode:
            raise LifecycleError(f"taskkill could not stop verified owned PID {pid}")
        return
    os.kill(pid, signal.SIGTERM)


def _force_stop(pid: int) -> None:
    if os.name == "nt":
        result = subprocess.run(["taskkill.exe", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True, timeout=5)
        if result.returncode:
            raise LifecycleError(f"taskkill failed for verified owned PID {pid}")
    else:
        os.kill(pid, signal.SIGKILL)


def _wait_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            return True
        time.sleep(0.05)
    return not _process_exists(pid)


def _validate_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or len(run_id) != 32 or any(ch not in "0123456789abcdef" for ch in run_id):
        raise ValueError("run ID must be a 32-character lowercase hexadecimal value")
