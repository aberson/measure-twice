from __future__ import annotations

import _thread
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import measure_twice.agent_bench.process as process_module
from measure_twice.agent_bench._linux_capabilities import LinuxCapabilityError
from measure_twice.agent_bench.process import (
    ProcessContractError,
    ProcessExecutionError,
    ProcessRequest,
    ProcessResourceLimits,
    run_process,
)

PYTHON = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
pytestmark = pytest.mark.linux_isolation


def _linux_fd_snapshot() -> dict[int, tuple[int, int, int, str]]:
    """Snapshot persistent process FDs without retaining the procfs enumeration FD."""

    assert sys.platform == "linux"
    snapshot: dict[int, tuple[int, int, int, str]] = {}
    for raw_fd in os.listdir("/proc/self/fd"):
        fd = int(raw_fd)
        try:
            metadata = os.fstat(fd)
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            # os.listdir's own short-lived descriptor is present in its result but closed before
            # this loop. A concurrently closed unrelated descriptor has the same bounded outcome.
            continue
        snapshot[fd] = (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode), target)
    return snapshot


def _assert_linux_fd_baseline(
    baseline: dict[int, tuple[int, int, int, str]],
) -> None:
    assert _linux_fd_snapshot() == baseline


def _request(
    tmp_path: Path,
    code: str,
    *,
    stdin: str = "",
    environment: dict[str, str] | None = None,
    timeout_s: float = 10.0,
    stream_limit_bytes: int = 64 * 1024,
    secret_values: tuple[str, ...] = (),
    resource_limits: ProcessResourceLimits | None = None,
    tree_root: Path | None = None,
) -> ProcessRequest:
    return ProcessRequest.create(
        argv=(PYTHON, "-I", "-c", code),
        stdin=stdin,
        cwd=tmp_path.resolve(),
        environment={} if environment is None else environment,
        timeout_s=timeout_s,
        stream_limit_bytes=stream_limit_bytes,
        secret_values=secret_values,
        resource_limits=resource_limits,
        tree_root=tree_root,
    )


def test_request_copies_mutable_inputs_and_is_frozen(tmp_path: Path) -> None:
    fd_baseline = _linux_fd_snapshot() if sys.platform == "linux" else None
    argv = [PYTHON, "-c", "pass"]
    environment = {"ONLY_ALLOWED": "original"}
    request = ProcessRequest.create(
        argv=argv,
        stdin="prompt",
        cwd=tmp_path.resolve(),
        environment=environment,
        timeout_s=1,
        stream_limit_bytes=100,
    )

    argv.append("changed")
    environment["ONLY_ALLOWED"] = "changed"
    assert request.argv == (PYTHON, "-c", "pass")
    assert request.environment == (("ONLY_ALLOWED", "original"),)
    rendered = repr(request)
    assert "prompt" not in rendered
    assert "original" not in rendered
    with pytest.raises(FrozenInstanceError):
        request.timeout_s = 2  # type: ignore[misc]
    request.close()
    if fd_baseline is not None:
        _assert_linux_fd_baseline(fd_baseline)


def test_process_request_is_one_shot(tmp_path: Path) -> None:
    request = _request(tmp_path, "pass")

    assert run_process(request).exit_code == 0
    with pytest.raises(ProcessContractError, match="one-shot and already consumed"):
        run_process(request)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux descriptor lifecycle invariant")
def test_linux_success_releases_every_request_descriptor(tmp_path: Path) -> None:
    baseline = _linux_fd_snapshot()

    result = run_process(_request(tmp_path, "print('complete')"))

    assert result.exit_code == 0
    assert result.stdout == b"complete\n"
    _assert_linux_fd_baseline(baseline)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux descriptor lifecycle invariant")
def test_linux_startup_failure_releases_every_request_descriptor(tmp_path: Path) -> None:
    baseline = _linux_fd_snapshot()
    invalid_executable = tmp_path / "invalid-executable"
    invalid_executable.write_bytes(b"not an executable image\n")
    invalid_executable.chmod(0o700)
    request = ProcessRequest.create(
        argv=(str(invalid_executable),),
        stdin="",
        cwd=tmp_path.resolve(),
        environment={},
        timeout_s=1,
        stream_limit_bytes=100,
    )

    with pytest.raises(ProcessExecutionError, match="could not start process executable"):
        run_process(request)

    _assert_linux_fd_baseline(baseline)


def _delayed_marker_code(marker: Path) -> str:
    return (
        "import pathlib,time; time.sleep(.6); "
        f"pathlib.Path({str(marker)!r}).write_text('escaped',encoding='utf-8')"
    )


@pytest.mark.skipif(sys.platform != "linux", reason="Linux post-spawn cleanup invariant")
@pytest.mark.parametrize("failing_thread", ["agent-bench-descendants", "agent-bench-stderr"])
def test_linux_partial_thread_start_failure_reaps_process_and_closes_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_thread: str,
) -> None:
    baseline = _linux_fd_snapshot()
    marker = tmp_path / f"{failing_thread}-escaped"
    spawned: list[subprocess.Popen[bytes]] = []
    original_popen = process_module.subprocess.Popen
    original_start = threading.Thread.start

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        proc = original_popen(*args, **kwargs)  # type: ignore[arg-type]
        spawned.append(proc)
        return proc

    def start_then_fail(thread: threading.Thread) -> None:
        original_start(thread)
        if thread.name == failing_thread:
            raise RuntimeError(f"injected {failing_thread} start failure")

    monkeypatch.setattr(process_module.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(threading.Thread, "start", start_then_fail)
    with pytest.raises(RuntimeError, match="injected"):
        run_process(_request(tmp_path, _delayed_marker_code(marker)))

    assert len(spawned) == 1
    assert spawned[0].poll() is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(spawned[0].pid, os.WNOHANG)
    time.sleep(0.7)
    assert not marker.exists()
    _assert_linux_fd_baseline(baseline)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux post-spawn cleanup invariant")
def test_linux_missing_root_identity_reaps_process_and_closes_fds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = _linux_fd_snapshot()
    marker = tmp_path / "missing-root-identity-escaped"
    spawned: list[subprocess.Popen[bytes]] = []
    original_popen = process_module.subprocess.Popen

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        proc = original_popen(*args, **kwargs)  # type: ignore[arg-type]
        spawned.append(proc)
        return proc

    monkeypatch.setattr(process_module.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(process_module, "_pid_starttime", lambda _pid: None)
    with pytest.raises(ProcessExecutionError, match="could not establish Linux process identity"):
        run_process(_request(tmp_path, _delayed_marker_code(marker)))

    assert len(spawned) == 1
    assert spawned[0].poll() is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(spawned[0].pid, os.WNOHANG)
    time.sleep(0.7)
    assert not marker.exists()
    _assert_linux_fd_baseline(baseline)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux descriptor lifecycle invariant")
def test_linux_request_hook_exception_releases_acquired_descriptor(tmp_path: Path) -> None:
    baseline = _linux_fd_snapshot()

    def abort_after_cwd_acquisition(stage: str) -> None:
        assert stage == "process-cwd"
        raise RuntimeError("caller aborted acquisition")

    with pytest.raises(RuntimeError, match="caller aborted acquisition"):
        ProcessRequest.create(
            argv=(PYTHON, "-I", "-c", "pass"),
            stdin="",
            cwd=tmp_path.resolve(),
            environment={},
            timeout_s=1,
            stream_limit_bytes=100,
            _race_hook=abort_after_cwd_acquisition,
        )

    _assert_linux_fd_baseline(baseline)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"argv": ("python",)}, "absolute executable"),
        ({"argv": ()}, "non-empty immutable tuple"),
        ({"environment": (("DUP", "1"), ("dup", "2"))}, "duplicate"),
        ({"environment": (("BAD=NAME", "1"),)}, "may not contain"),
        ({"timeout_s": float("inf")}, "finite and positive"),
        ({"stream_limit_bytes": True}, "positive integer"),
        ({"secret_values": ("",)}, "non-empty string"),
    ],
)
def test_request_rejects_ambiguous_or_mutable_contract_fields(
    tmp_path: Path, kwargs: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "argv": (PYTHON, "-c", "pass"),
        "stdin": "",
        "cwd": tmp_path.resolve(),
        "environment": (),
        "timeout_s": 1.0,
        "stream_limit_bytes": 100,
    }
    values.update(kwargs)

    with pytest.raises(ProcessContractError, match=message):
        ProcessRequest(**values)  # type: ignore[arg-type]


def test_exact_utf8_stdin_cwd_and_allowlisted_environment(tmp_path: Path) -> None:
    prompt = "snowman=☃; CJK=漢字; emoji=🧪\n"
    code = (
        "import json,os,sys; pathlib=__import__('pathlib'); "
        "raw=(pathlib.Path('/proc/self/environ').read_bytes() "
        "if pathlib.Path('/proc/self/environ').exists() else "
        "b'\\0'.join(f'{k}={v}'.encode() for k,v in os.environ.items())); "
        "environment=dict(item.decode('utf-8').split('=',1) "
        "for item in raw.split(b'\\0') if item); "
        "payload=sys.stdin.buffer.read(); "
        "sys.stdout.buffer.write(payload); "
        "sys.stderr.buffer.write(json.dumps({"
        "'cwd':os.getcwd(),'environment':environment},"
        "ensure_ascii=False,sort_keys=True).encode('utf-8'))"
    )
    result = run_process(
        _request(
            tmp_path,
            code,
            stdin=prompt,
            environment={"ONLY_ALLOWED": "café", "EMPTY_ALLOWED": ""},
        )
    )

    assert result.termination == "exited"
    assert result.exit_code == 0
    assert result.signal is None
    assert result.stdout == prompt.encode("utf-8")
    metadata = json.loads(result.stderr.decode("utf-8"))
    assert metadata == {
        "cwd": str(tmp_path.resolve()),
        "environment": {"EMPTY_ALLOWED": "", "ONLY_ALLOWED": "café"},
    }


@pytest.mark.skipif(sys.platform != "linux", reason="Linux FD-pinned cwd invariant")
def test_linux_cwd_rename_to_symlink_race_uses_acquired_inode(tmp_path: Path) -> None:
    baseline = _linux_fd_snapshot()
    cwd = tmp_path / "working"
    acquired_cwd = tmp_path / "acquired-working"
    replacement = tmp_path / "replacement"
    cwd.mkdir()
    replacement.mkdir()
    (cwd / "original-marker").write_text("original", encoding="utf-8")
    (replacement / "replacement-canary").write_text("canary", encoding="utf-8")
    acquisition_barrier = threading.Barrier(2)
    replacement_barrier = threading.Barrier(2)
    mutation_errors: list[BaseException] = []

    def replace_pathname() -> None:
        try:
            acquisition_barrier.wait(timeout=5)
            cwd.rename(acquired_cwd)
            cwd.symlink_to(replacement, target_is_directory=True)
        except BaseException as exc:
            mutation_errors.append(exc)
        finally:
            replacement_barrier.wait(timeout=5)

    def pause_after_acquisition(stage: str) -> None:
        assert stage == "process-cwd"
        acquisition_barrier.wait(timeout=5)
        replacement_barrier.wait(timeout=5)

    mutator = threading.Thread(target=replace_pathname, name="replace-process-cwd")
    mutator.start()
    try:
        request = ProcessRequest.create(
            argv=(
                PYTHON,
                "-I",
                "-c",
                "import json,os; print(json.dumps(sorted(os.listdir('.'))))",
            ),
            stdin="",
            cwd=cwd.resolve(),
            environment={},
            timeout_s=2,
            stream_limit_bytes=1024,
            _race_hook=pause_after_acquisition,
        )
    finally:
        mutator.join(timeout=5)

    assert not mutator.is_alive()
    assert mutation_errors == []
    assert cwd.is_symlink()
    result = run_process(request)

    assert result.termination == "exited"
    assert result.exit_code == 0
    assert json.loads(result.stdout) == ["original-marker"]
    assert b"replacement-canary" not in result.stdout
    assert b"canary" not in result.stderr
    _assert_linux_fd_baseline(baseline)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux pinned executable invariant")
def test_linux_process_create_allows_trusted_ordinary_final_executable_symlink(
    tmp_path: Path,
) -> None:
    baseline = _linux_fd_snapshot()
    executable = tmp_path / "trusted-executable"
    executable.write_text("#!/bin/sh\nprintf trusted-final-symlink\n", encoding="utf-8")
    executable.chmod(0o700)
    executable_symlink = tmp_path / "trusted-executable-link"
    executable_symlink.symlink_to(executable.name)
    request = ProcessRequest.create(
        argv=(str(executable_symlink),),
        stdin="",
        cwd=tmp_path.resolve(),
        environment={},
        timeout_s=2,
        stream_limit_bytes=1024,
    )

    result = run_process(request)

    assert result.exit_code == 0
    assert result.stdout == b"trusted-final-symlink"
    _assert_linux_fd_baseline(baseline)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux pinned executable invariant")
def test_linux_process_create_denies_out_of_root_final_executable_symlink(
    tmp_path: Path,
) -> None:
    baseline = _linux_fd_snapshot()
    outside_executable = tmp_path.parent / f"{tmp_path.name}-outside-executable"
    outside_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    outside_executable.chmod(0o700)
    escaping_symlink = tmp_path / "escaping-executable"
    escaping_symlink.symlink_to(Path("..") / outside_executable.name)

    with pytest.raises(LinuxCapabilityError, match="could not acquire FD capability"):
        ProcessRequest.create(
            argv=(str(escaping_symlink),),
            stdin="",
            cwd=tmp_path.resolve(),
            environment={},
            timeout_s=2,
            stream_limit_bytes=1024,
        )

    _assert_linux_fd_baseline(baseline)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux pinned executable invariant")
def test_linux_process_create_denies_procfs_magic_executable_symlink(tmp_path: Path) -> None:
    executable_fd = os.open(PYTHON, os.O_RDONLY)
    try:
        magic_link = Path(f"/proc/{os.getpid()}/fd/{executable_fd}")
        with pytest.raises(LinuxCapabilityError, match="could not acquire FD capability"):
            ProcessRequest.create(
                argv=(str(magic_link),),
                stdin="",
                cwd=tmp_path.resolve(),
                environment={},
                timeout_s=2,
                stream_limit_bytes=1024,
            )
    finally:
        os.close(executable_fd)


def test_capture_preserves_non_utf8_bytes(tmp_path: Path) -> None:
    result = run_process(_request(tmp_path, "import os; os.write(1, bytes([0,255,128,10]))"))

    assert result.stdout == b"\x00\xff\x80\n"
    assert result.termination == "exited"


@pytest.mark.parametrize(("fd", "field"), [(1, "stdout"), (2, "stderr")])
def test_stream_ceiling_terminates_and_preserves_only_bounded_bytes(
    tmp_path: Path, fd: int, field: str
) -> None:
    fd_baseline = _linux_fd_snapshot() if sys.platform == "linux" else None
    code = f"import os,time; os.write({fd}, b'x'*4096); time.sleep(30)"
    result = run_process(_request(tmp_path, code, stream_limit_bytes=64))

    assert result.termination == "stream-limit"
    assert result.exit_code is None
    assert len(getattr(result, field)) == 64
    assert getattr(result, f"{field}_limit_exceeded") is True
    other = "stderr" if field == "stdout" else "stdout"
    assert len(getattr(result, other)) <= 64
    if fd_baseline is not None:
        _assert_linux_fd_baseline(fd_baseline)


def test_exact_stream_ceiling_is_not_an_overflow(tmp_path: Path) -> None:
    result = run_process(
        _request(tmp_path, "import os; os.write(1, b'x'*64)", stream_limit_bytes=64)
    )

    assert result.termination == "exited"
    assert result.stdout == b"x" * 64
    assert result.stdout_limit_exceeded is False


@pytest.mark.parametrize(("fd", "field"), [(1, "stdout"), (2, "stderr")])
def test_secret_value_is_exactly_redacted_at_capture_boundary(
    tmp_path: Path, fd: int, field: str
) -> None:
    secret = "credential-sentinel-0123456789"  # noqa: S105 - deliberately fake canary value.
    prefix = b"prefix:"
    expected = prefix + b"<redacted>"
    code = f"import os; os.write({fd}, {prefix!r} + {secret.encode()!r} + b'trailing')"
    result = run_process(
        _request(
            tmp_path,
            code,
            stream_limit_bytes=len(expected),
            secret_values=(secret,),
        )
    )

    assert result.termination == "stream-limit"
    assert getattr(result, field) == expected
    assert getattr(result, f"{field}_secret_detected") is True
    other = "stderr" if field == "stdout" else "stdout"
    assert getattr(result, other) == b""
    assert getattr(result, f"{other}_secret_detected") is False
    assert secret not in repr(result)


def test_secret_detection_survives_bytes_far_beyond_persisted_ceiling(tmp_path: Path) -> None:
    secret = "far-beyond-ceiling-credential"  # noqa: S105 - deliberately fake canary value.
    code = f"import os; os.write(1, b'x'*4096 + {secret.encode()!r});"
    result = run_process(_request(tmp_path, code, stream_limit_bytes=24, secret_values=(secret,)))

    assert result.stream_limit_exceeded is True
    assert len(result.stdout) == 24
    assert secret.encode("utf-8") not in result.stdout
    assert result.stdout_secret_detected is True


@pytest.mark.skipif(sys.platform != "linux", reason="Linux PID namespace ownership invariant")
def test_unrelated_concurrent_harness_child_is_not_owned_or_reaped(tmp_path: Path) -> None:
    benchmark_ready = tmp_path / "benchmark-ready"
    unrelated_started = threading.Event()
    unrelated: list[subprocess.Popen[bytes]] = []

    def start_unrelated_after_benchmark() -> None:
        deadline = time.monotonic() + 5
        while not benchmark_ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not benchmark_ready.exists():
            return
        proc = subprocess.Popen(
            ("/bin/sleep", "30"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        unrelated.append(proc)
        unrelated_started.set()

    starter = threading.Thread(target=start_unrelated_after_benchmark, name="unrelated-child")
    starter.start()
    try:
        code = (
            "import pathlib,time; "
            f"pathlib.Path({str(benchmark_ready)!r}).write_text('ready',encoding='utf-8'); "
            "time.sleep(.5)"
        )
        result = run_process(_request(tmp_path, code, timeout_s=3))
        starter.join(timeout=5)

        assert result.exit_code == 0
        assert unrelated_started.is_set()
        assert len(unrelated) == 1
        assert unrelated[0].poll() is None
    finally:
        starter.join(timeout=5)
        for proc in unrelated:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux PID namespace status protocol")
@pytest.mark.parametrize(
    ("code", "termination", "exit_code", "signal_number"),
    [
        ("raise SystemExit(23)", "exited", 23, None),
        (
            "import os,signal; os.kill(os.getpid(),signal.SIGTERM)",
            "signalled",
            None,
            signal.SIGTERM,
        ),
    ],
)
def test_namespace_supervisor_preserves_exact_target_status(
    tmp_path: Path,
    code: str,
    termination: str,
    exit_code: int | None,
    signal_number: signal.Signals | None,
) -> None:
    result = run_process(_request(tmp_path, code))

    assert result.termination == termination
    assert result.exit_code == exit_code
    assert result.signal == signal_number


def _tree_code(ready: Path, leaked: Path) -> str:
    child = (
        "import os,sys,time; "
        "os.setsid() if os.name == 'posix' else None; "
        "open(sys.argv[1], 'w', encoding='utf-8').write('ready'); "
        "time.sleep(1.0); "
        "open(sys.argv[2], 'w', encoding='utf-8').write('leaked')"
    )
    return (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-I','-c',{child!r},{str(ready)!r},{str(leaked)!r}]); "
        f"deadline=time.monotonic()+5; p={str(ready)!r}; "
        "\nwhile not __import__('os').path.exists(p) "
        "and time.monotonic()<deadline: time.sleep(.01)\n"
        "time.sleep(30)"
    )


def _leader_exits_first_code(ready: Path, leaked: Path) -> str:
    code = _tree_code(ready, leaked)
    return code.rsplit("time.sleep(30)", 1)[0] + "pass"


def _double_fork_code(ready: Path, leaked: Path, *, hold_leader: bool) -> str:
    leader_tail = "time.sleep(30)" if hold_leader else "pass"
    return (
        "import os,time\n"
        "pid=os.fork()\n"
        "if pid == 0:\n"
        " os.setsid()\n"
        " if os.fork(): os._exit(0)\n"
        f" open({str(ready)!r},'w',encoding='utf-8').write('ready')\n"
        " null=os.open(os.devnull,os.O_RDWR)\n"
        " [os.dup2(null,fd) for fd in (0,1,2)]\n"
        " time.sleep(1.0)\n"
        f" open({str(leaked)!r},'w',encoding='utf-8').write('leaked')\n"
        " os._exit(0)\n"
        f"deadline=time.monotonic()+5; marker={str(ready)!r}\n"
        "while not os.path.exists(marker) and time.monotonic()<deadline: time.sleep(.01)\n"
        f"{leader_tail}\n"
    )


def test_timeout_kills_detached_descendant_before_it_can_escape(tmp_path: Path) -> None:
    fd_baseline = _linux_fd_snapshot() if sys.platform == "linux" else None
    ready = tmp_path / "ready"
    leaked = tmp_path / "leaked"
    result = run_process(
        _request(tmp_path, _tree_code(ready, leaked), timeout_s=0.25, stream_limit_bytes=1024)
    )

    assert result.termination == "timeout"
    assert result.exit_code is None
    assert ready.is_file()
    time.sleep(1.1)
    assert not leaked.exists()
    if fd_baseline is not None:
        _assert_linux_fd_baseline(fd_baseline)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux PID namespace descendant invariant")
def test_namespace_owner_cleans_descendant_when_direct_child_exits(tmp_path: Path) -> None:
    ready = tmp_path / "orphan-ready"
    leaked = tmp_path / "orphan-leaked"
    started = time.monotonic()
    result = run_process(
        _request(
            tmp_path,
            _leader_exits_first_code(ready, leaked),
            timeout_s=0.25,
            stream_limit_bytes=1024,
        )
    )

    assert result.termination == "exited"
    assert result.exit_code == 0
    assert time.monotonic() - started < 0.9
    assert ready.is_file()
    time.sleep(1.1)
    assert not leaked.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux PID namespace descendant invariant")
def test_descendant_owner_structurally_contains_every_reparented_session(
    tmp_path: Path,
) -> None:
    ready_paths: list[Path] = []
    leaked_paths: list[Path] = []
    for index in range(3):
        direct_ready = tmp_path / f"direct-ready-{index}"
        direct_leaked = tmp_path / f"direct-leaked-{index}"
        forked_ready = tmp_path / f"forked-ready-{index}"
        forked_leaked = tmp_path / f"forked-leaked-{index}"
        detach = (
            "import os,time; os.setsid(); "
            f"open({str(direct_ready)!r},'w',encoding='utf-8').write('ready'); "
            "null=os.open(os.devnull,os.O_RDWR); "
            "[os.dup2(null,fd) for fd in (0,1,2)]; "
            "time.sleep(.8); "
            f"open({str(direct_leaked)!r},'w',encoding='utf-8').write('leaked')"
        )
        double_fork = (
            "import os,time; "
            "pid=os.fork(); "
            "\nif pid: os._exit(0)\n"
            "os.setsid(); pid=os.fork(); "
            "\nif pid: os._exit(0)\n"
            f"open({str(forked_ready)!r},'w',encoding='utf-8').write('ready'); "
            "null=os.open(os.devnull,os.O_RDWR); "
            "[os.dup2(null,fd) for fd in (0,1,2)]; "
            "time.sleep(.8); "
            f"open({str(forked_leaked)!r},'w',encoding='utf-8').write('leaked')"
        )
        leader = (
            "import os,subprocess,sys,time; "
            f"subprocess.Popen([sys.executable,'-I','-c',{detach!r}]); "
            f"subprocess.Popen([sys.executable,'-I','-c',{double_fork!r}]); "
            f"paths=({str(direct_ready)!r},{str(forked_ready)!r}); "
            "deadline=time.monotonic()+5; "
            "\nwhile not all(os.path.exists(path) for path in paths) "
            "and time.monotonic()<deadline: time.sleep(.01)\n"
        )
        result = run_process(_request(tmp_path, leader, timeout_s=0.5, stream_limit_bytes=1024))
        assert result.termination == "exited"
        ready_paths.extend((direct_ready, forked_ready))
        leaked_paths.extend((direct_leaked, forked_leaked))

    assert all(path.is_file() for path in ready_paths)
    time.sleep(0.9)
    assert not any(path.exists() for path in leaked_paths)


@pytest.mark.skipif(sys.platform != "linux", reason="double-fork requires Linux")
def test_timeout_kills_double_fork_descendant(tmp_path: Path) -> None:
    ready = tmp_path / "double-fork-timeout-ready"
    leaked = tmp_path / "double-fork-timeout-leaked"

    result = run_process(
        _request(
            tmp_path,
            _double_fork_code(ready, leaked, hold_leader=True),
            timeout_s=0.5,
            stream_limit_bytes=1024,
        )
    )

    assert result.termination == "timeout"
    assert ready.is_file()
    time.sleep(1.1)
    assert not leaked.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="double-fork requires Linux")
def test_keyboard_interrupt_kills_double_fork_tree_then_propagates(tmp_path: Path) -> None:
    fd_baseline = _linux_fd_snapshot() if sys.platform == "linux" else None
    ready = tmp_path / "interrupt-ready"
    leaked = tmp_path / "interrupt-leaked"
    interrupt_sent = threading.Event()

    def interrupt_after_ready() -> None:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if ready.exists():
            interrupt_sent.set()
            _thread.interrupt_main()

    trigger = threading.Thread(target=interrupt_after_ready, name="interrupt-after-ready")
    trigger.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            run_process(
                _request(
                    tmp_path,
                    _double_fork_code(ready, leaked, hold_leader=True),
                    timeout_s=10,
                    stream_limit_bytes=1024,
                )
            )
    finally:
        trigger.join(timeout=6)

    assert interrupt_sent.is_set()
    assert ready.is_file()
    time.sleep(1.1)
    assert not leaked.exists()
    if fd_baseline is not None:
        _assert_linux_fd_baseline(fd_baseline)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux monitor fail-closed invariant")
def test_descendant_monitor_exception_fails_closed_and_kills_target(tmp_path: Path) -> None:
    baseline = _linux_fd_snapshot()
    marker = tmp_path / "monitor-failure-escaped"
    (tmp_path / "scan-me").write_text("seed", encoding="utf-8")

    def fail_tree_scan(_relative: str) -> None:
        raise RuntimeError("injected live monitor failure")

    request = ProcessRequest.create(
        argv=(PYTHON, "-I", "-c", _delayed_marker_code(marker)),
        stdin="",
        cwd=tmp_path.resolve(),
        environment={},
        timeout_s=5,
        stream_limit_bytes=1024,
        resource_limits=ProcessResourceLimits(tree_files=10, tree_bytes=1024),
        tree_root=tmp_path.resolve(),
        _tree_before_open=fail_tree_scan,
    )

    with pytest.raises(ProcessExecutionError, match="Linux descendant monitor failed"):
        run_process(request)

    time.sleep(0.7)
    assert not marker.exists()
    _assert_linux_fd_baseline(baseline)


@pytest.mark.skipif(os.name != "posix", reason="POSIX rlimit contract")
def test_file_size_rlimit_is_inherited_by_child(tmp_path: Path) -> None:
    result = run_process(
        _request(
            tmp_path,
            "open('too-large.bin','wb').write(b'x'*8192)",
            resource_limits=ProcessResourceLimits(file_bytes=1024),
        )
    )

    assert result.exit_code != 0 or result.signal is not None
    assert (tmp_path / "too-large.bin").stat().st_size <= 1024


@pytest.mark.skipif(sys.platform != "linux", reason="Linux aggregate descendant monitor")
def test_cpu_ceiling_counts_short_lived_reaped_children(tmp_path: Path) -> None:
    worker = (
        "import time; deadline=time.process_time()+.035; value=1; "
        "\nwhile time.process_time()<deadline: value=(value*3+1)%1000000007"
    )
    code = (
        "import subprocess,sys; "
        f"worker={worker!r}; "
        "[subprocess.run([sys.executable,'-I','-c',worker],check=True) for _ in range(60)]"
    )

    result = run_process(
        _request(
            tmp_path,
            code,
            timeout_s=10,
            resource_limits=ProcessResourceLimits(
                cpu_seconds=1,
                memory_bytes=512 * 1024 * 1024,
                processes=8,
            ),
        )
    )

    assert result.termination == "resource-limit"
    assert result.resource_limit == "cpu"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux aggregate descendant monitor")
@pytest.mark.parametrize(
    ("resource_name", "code", "limits"),
    [
        (
            "cpu",
            (
                "import subprocess,sys,time; "
                "child='value=1\\nwhile True: value=(value*3+1)%1000000007'; "
                "[subprocess.Popen([sys.executable,'-I','-c',child]) for _ in range(2)]; "
                "time.sleep(30)"
            ),
            ProcessResourceLimits(
                cpu_seconds=1,
                memory_bytes=512 * 1024 * 1024,
                processes=32,
            ),
        ),
        (
            "memory",
            (
                "import subprocess,sys,time; "
                "child='import time; blocks=[bytearray(8*1024*1024) for _ in range(5)]; "
                "time.sleep(30)'; "
                "[subprocess.Popen([sys.executable,'-I','-c',child]) for _ in range(2)]; "
                "time.sleep(30)"
            ),
            ProcessResourceLimits(
                cpu_seconds=10,
                memory_bytes=80 * 1024 * 1024,
                processes=32,
            ),
        ),
        (
            "processes",
            (
                "import subprocess,sys,time; child='import time; time.sleep(30)'; "
                "[subprocess.Popen([sys.executable,'-I','-c',child]) for _ in range(20)]; "
                "time.sleep(30)"
            ),
            ProcessResourceLimits(
                cpu_seconds=10,
                memory_bytes=512 * 1024 * 1024,
                processes=5,
            ),
        ),
    ],
)
def test_linux_resource_monitor_enforces_aggregate_descendant_ceiling(
    tmp_path: Path,
    resource_name: str,
    code: str,
    limits: ProcessResourceLimits,
) -> None:
    fd_baseline = _linux_fd_snapshot()
    result = run_process(
        _request(
            tmp_path,
            code,
            timeout_s=5,
            resource_limits=limits,
        )
    )

    assert result.termination == "resource-limit"
    assert result.resource_limit == resource_name
    assert result.elapsed_ms < 4_000
    _assert_linux_fd_baseline(fd_baseline)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux live evaluator-tree monitor")
@pytest.mark.parametrize(
    ("count", "size", "files", "tree_bytes", "resource_name"),
    [(6, 10, 5, 1_000, "file-count"), (3, 50, 10, 100, "file-bytes")],
)
def test_linux_resource_monitor_terminates_live_tree_ceiling_crossing(
    tmp_path: Path,
    count: int,
    size: int,
    files: int,
    tree_bytes: int,
    resource_name: str,
) -> None:
    fd_baseline = _linux_fd_snapshot()
    code = (
        "import pathlib,time; "
        f"[(pathlib.Path(f'created-{{index}}').write_bytes(b'x'*{size})) "
        f"for index in range({count})]; time.sleep(30)"
    )
    result = run_process(
        _request(
            tmp_path,
            code,
            timeout_s=5,
            resource_limits=ProcessResourceLimits(
                file_bytes=max(size, tree_bytes),
                tree_files=files,
                tree_bytes=tree_bytes,
            ),
            tree_root=tmp_path,
        )
    )

    assert result.termination == "resource-limit"
    assert result.resource_limit == resource_name
    _assert_linux_fd_baseline(fd_baseline)


def test_resource_limits_fail_closed_on_non_posix(tmp_path: Path) -> None:
    request = _request(
        tmp_path,
        "pass",
        resource_limits=ProcessResourceLimits(cpu_seconds=1),
    )
    if os.name == "posix":
        assert run_process(request).exit_code == 0
    else:
        with pytest.raises(ProcessContractError, match="require a POSIX host"):
            run_process(request)
