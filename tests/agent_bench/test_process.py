from __future__ import annotations

import _thread
import array
import errno
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import measure_twice.agent_bench.process as process_module
from measure_twice.agent_bench._linux_capabilities import (
    LinuxCapabilityError,
    LinuxPathCapability,
)
from measure_twice.agent_bench.process import (
    EvaluatorScratch,
    LinuxResourceGuard,
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


def _handshake_scratch(tmp_path: Path) -> EvaluatorScratch:
    """Build the smallest valid owned scratch configuration for handshake-only tests."""

    source = tmp_path / "handshake-seed"
    source.mkdir()
    source_capability = LinuxPathCapability.acquire_absolute(source, expected="directory")
    page_size = int(os.sysconf("SC_PAGE_SIZE"))
    return EvaluatorScratch(
        source=source_capability,
        file_limit=1,
        byte_limit=1,
        tmpfs_bytes=2 * page_size,
        tmpfs_inodes=10_002,
    )


def _send_fds(control: socket.socket, payload: bytes, fds: list[int]) -> None:
    control.sendmsg(
        [payload],
        [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))],
    )


class _HandshakeGuardState:
    """Minimal cleanup-owning state used to exercise the real FD receive boundary."""

    def __init__(self, capability: LinuxPathCapability, kill_fd: int) -> None:
        self.capability = capability
        self.kill_fd = kill_fd
        self.validated = False
        self.closed = False

    def validate_before_release(self) -> None:
        self.validated = True

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        os.close(self.kill_fd)
        self.capability.close()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux SCM_RIGHTS handshake invariant")
def test_linux_evaluator_handshake_requires_three_cloexec_capabilities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual receive boundary gets exactly cgroup/scratch/kill FDs before release."""

    baseline = _linux_fd_snapshot()
    cgroup_directory = tmp_path / "cgroup"
    scratch_directory = tmp_path / "scratch"
    cgroup_directory.mkdir()
    scratch_directory.mkdir()
    kill_path = tmp_path / "kill"
    kill_path.write_bytes(b"")
    cgroup_fd = os.open(cgroup_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    scratch_fd = os.open(scratch_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    kill_fd = os.open(kill_path, os.O_WRONLY | os.O_CLOEXEC)
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    scratch = _handshake_scratch(tmp_path)
    request = SimpleNamespace(
        resource_guard=LinuxResourceGuard(1, 1, 100),
        _evaluator_scratch=scratch,
        timeout_s=1.0,
    )
    runtime = SimpleNamespace(resource_guard=None)
    adopted: list[_HandshakeGuardState] = []

    def adopt(
        capability: LinuxPathCapability,
        received_kill_fd: int,
        _configuration: LinuxResourceGuard,
        *,
        scope_relative_path: str,
    ) -> _HandshakeGuardState:
        assert scope_relative_path == "/user.slice/test.scope"
        state = _HandshakeGuardState(capability, received_kill_fd)
        adopted.append(state)
        return state

    monkeypatch.setattr(
        process_module._LinuxResourceGuardState,
        "adopt",
        staticmethod(adopt),
    )
    monkeypatch.setattr(process_module, "_validate_evaluator_tmpfs", lambda *_args: None)
    monkeypatch.setattr(process_module, "copy_tree", lambda *_args, **_kwargs: None)
    try:
        _send_fds(child, b"MT26R", [cgroup_fd, scratch_fd, kill_fd])
        received_scratch = process_module._receive_evaluator_handshake(
            parent,
            request,
            runtime,
            "/user.slice/test.scope",
        )
        try:
            assert len(adopted) == 1
            state = adopted[0]
            assert runtime.resource_guard is state
            assert state.validated is True
            assert os.get_inheritable(state.capability.fd) is False
            assert os.get_inheritable(state.kill_fd) is False
            assert os.get_inheritable(received_scratch.fd) is False
        finally:
            received_scratch.close()
            scratch.close()
            adopted[0].close()
            runtime.resource_guard = None
    finally:
        parent.close()
        child.close()
        os.close(cgroup_fd)
        os.close(scratch_fd)
        os.close(kill_fd)
    _assert_linux_fd_baseline(baseline)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux SCM_RIGHTS handshake invariant")
def test_linux_evaluator_handshake_budget_is_independent_of_the_caller_wall_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-release bring-up is bounded by its own budget, never by the target's clock.

    ``timeout_s`` is the SUBMITTED CODE's wall-clock budget, consumed only after the release
    barrier.  The five-stage bring-up this socket covers -- systemd-run scope creation, cgroup
    delegation and readback, the bounded private tmpfs, supervisor binding, the FD handshake --
    runs before any submitted byte executes, so clamping it by ``timeout_s`` made a slow-but-
    healthy host report evaluator infrastructure as unavailable.

    Calibration (measurement-validity.md): ``timeout_s`` is deliberately far SMALLER than the
    setup budget.  Against the previous ``min(SANDBOX_SETUP_TIMEOUT_S, timeout_s)`` this test is
    red -- the error would arrive in ~0.05 s and the elapsed lower bound would fail -- so it
    discriminates the fix rather than restating it.  It is also the first coverage this error
    path has ever had.
    """

    baseline = _linux_fd_snapshot()
    monkeypatch.setattr(process_module, "SANDBOX_SETUP_TIMEOUT_S", 0.5)
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    scratch = _handshake_scratch(tmp_path)
    request = SimpleNamespace(
        resource_guard=LinuxResourceGuard(1, 1, 100),
        _evaluator_scratch=scratch,
        timeout_s=0.05,
    )
    runtime = SimpleNamespace(resource_guard=None)
    try:
        # Nothing is ever sent on `child`: the supervisor is modelled as wedged before its
        # pre-release handshake, which is exactly the liveness failure the budget exists for.
        begin = time.monotonic()
        with pytest.raises(ProcessExecutionError, match="did not reach its release barrier"):
            process_module._receive_evaluator_handshake(
                parent,
                request,
                runtime,
                "/user.slice/test.scope",
            )
        elapsed = time.monotonic() - begin
        assert elapsed >= 0.4, (
            f"handshake honoured the caller clock, not the setup budget: {elapsed}"
        )
    finally:
        scratch.close()
        parent.close()
        child.close()
    _assert_linux_fd_baseline(baseline)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux SCM_RIGHTS handshake invariant")
@pytest.mark.parametrize(("payload", "fd_count"), [(b"wrong", 3), (b"MT26R", 2)])
def test_linux_evaluator_handshake_rejects_malformed_message_before_release(
    tmp_path: Path,
    payload: bytes,
    fd_count: int,
) -> None:
    baseline = _linux_fd_snapshot()
    directory = tmp_path / "received"
    directory.mkdir()
    fds = [
        os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        for _index in range(fd_count)
    ]
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    scratch = _handshake_scratch(tmp_path)
    request = SimpleNamespace(
        resource_guard=LinuxResourceGuard(1, 1, 100),
        _evaluator_scratch=scratch,
        timeout_s=1.0,
    )
    runtime = SimpleNamespace(resource_guard=None)
    try:
        _send_fds(child, payload, fds)
        with pytest.raises(ProcessExecutionError, match="invalid FD handshake"):
            process_module._receive_evaluator_handshake(
                parent,
                request,
                runtime,
                "/user.slice/test.scope",
            )
        assert runtime.resource_guard is None
    finally:
        scratch.close()
        parent.close()
        child.close()
        for fd in fds:
            os.close(fd)
    _assert_linux_fd_baseline(baseline)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux evaluator startup ownership invariant")
@pytest.mark.parametrize(
    "failure_stage",
    ["guard", "scratch-fd", "scratch-readback", "copy", "terminal-adoption"],
)
def test_linux_evaluator_post_handshake_failure_keeps_verified_guard_for_common_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    """Every failure after cgroup adoption leaves the pinned scope owned by runtime cleanup."""

    baseline = _linux_fd_snapshot()
    cgroup_directory = tmp_path / "cgroup"
    scratch_directory = tmp_path / "scratch"
    cgroup_directory.mkdir()
    scratch_directory.mkdir()
    kill_path = tmp_path / "kill"
    kill_path.write_bytes(b"")
    invalid_scratch = tmp_path / "not-a-directory"
    invalid_scratch.write_bytes(b"x")
    cgroup_fd = os.open(cgroup_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    scratch_fd = os.open(
        invalid_scratch if failure_stage == "scratch-fd" else scratch_directory,
        os.O_RDONLY | os.O_CLOEXEC | (0 if failure_stage == "scratch-fd" else os.O_DIRECTORY),
    )
    kill_fd = os.open(kill_path, os.O_WRONLY | os.O_CLOEXEC)
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    scratch = _handshake_scratch(tmp_path)
    request = SimpleNamespace(
        resource_guard=LinuxResourceGuard(1, 1, 100),
        _evaluator_scratch=scratch,
        timeout_s=1.0,
    )
    runtime = SimpleNamespace(resource_guard=None)
    adopted: list[_HandshakeGuardState] = []

    class FailingState(_HandshakeGuardState):
        def validate_before_release(self) -> None:
            super().validate_before_release()
            if failure_stage == "guard":
                raise ProcessExecutionError("injected guard readback mismatch")

    def adopt(
        capability: LinuxPathCapability,
        received_kill_fd: int,
        _configuration: LinuxResourceGuard,
        *,
        scope_relative_path: str,
    ) -> _HandshakeGuardState:
        assert scope_relative_path == "/user.slice/test.scope"
        state = FailingState(capability, received_kill_fd)
        adopted.append(state)
        return state

    monkeypatch.setattr(
        process_module._LinuxResourceGuardState,
        "adopt",
        staticmethod(adopt),
    )
    if failure_stage == "scratch-readback":
        monkeypatch.setattr(
            process_module,
            "_validate_evaluator_tmpfs",
            lambda *_args: (_ for _ in ()).throw(
                ProcessExecutionError("injected tmpfs readback mismatch")
            ),
        )
    else:
        monkeypatch.setattr(process_module, "_validate_evaluator_tmpfs", lambda *_args: None)
    if failure_stage == "copy":
        monkeypatch.setattr(
            process_module,
            "copy_tree",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                LinuxCapabilityError("injected copy failure")
            ),
        )
    else:
        monkeypatch.setattr(process_module, "copy_tree", lambda *_args, **_kwargs: None)
    if failure_stage == "terminal-adoption":
        monkeypatch.setattr(
            EvaluatorScratch,
            "adopt_terminal_tree",
            lambda _self, capability: (
                capability.close(),
                (_ for _ in ()).throw(ProcessExecutionError("injected terminal adoption failure")),
            )[1],
        )
    try:
        _send_fds(child, b"MT26R", [cgroup_fd, scratch_fd, kill_fd])
        with pytest.raises(ProcessExecutionError):
            process_module._receive_evaluator_handshake(
                parent,
                request,
                runtime,
                "/user.slice/test.scope",
            )
        assert len(adopted) == 1
        assert runtime.resource_guard is adopted[0]
        assert adopted[0].closed is False
    finally:
        if adopted:
            adopted[0].close()
        runtime.resource_guard = None
        scratch.close()
        parent.close()
        child.close()
        os.close(cgroup_fd)
        os.close(scratch_fd)
        os.close(kill_fd)
    _assert_linux_fd_baseline(baseline)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux failed-start owner reap invariant")
@pytest.mark.parametrize("reap_outcome", ["timeout", "oserror", "clean"])
def test_linux_failed_start_direct_owner_reap_failure_surfaces_as_execution_error(
    monkeypatch: pytest.MonkeyPatch,
    reap_outcome: str,
) -> None:
    """A systemd-run owner that survives failed-start termination is an execution error."""

    baseline = _linux_fd_snapshot()

    class _SurvivingDirectOwner:
        def __init__(self) -> None:
            self.pid = 0x7FFFFFFE
            self.stdin = os.fdopen(os.open(os.devnull, os.O_WRONLY | os.O_CLOEXEC), "wb")
            self.stdout = os.fdopen(os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC), "rb")
            self.stderr = os.fdopen(os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC), "rb")

        def poll(self) -> int | None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            if reap_outcome == "timeout":
                raise subprocess.TimeoutExpired("systemd-run", timeout)
            if reap_outcome == "oserror":
                raise OSError(errno.ECHILD, "no child processes")
            return 0

    owner = _SurvivingDirectOwner()
    kill_calls: list[tuple[object, object, object]] = []
    monkeypatch.setattr(
        process_module,
        "_kill_tree",
        lambda *args: kill_calls.append(args[:3]),
    )
    try:
        error = process_module._cleanup_failed_linux_start(
            cast("subprocess.Popen[bytes]", owner),
            None,
            "",
        )
        # The direct owner is signalled before any bounded wait, on every arm.
        assert kill_calls == [(owner, owner.pid, None)]
        if reap_outcome == "timeout":
            assert isinstance(error, ProcessExecutionError)
            assert "direct child survived failed-start termination" in str(error)
        elif reap_outcome == "oserror":
            assert isinstance(error, OSError)
            assert error.errno == errno.ECHILD
        else:
            assert error is None
        # A failed reap must still release every inherited pipe.
        assert owner.stdin.closed and owner.stdout.closed and owner.stderr.closed
    finally:
        for stream in (owner.stdin, owner.stdout, owner.stderr):
            if not stream.closed:
                stream.close()
    _assert_linux_fd_baseline(baseline)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux no-handshake absence-interval invariant")
def test_linux_failed_start_without_handshake_proves_a_full_absence_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No descriptor crossed the boundary, so absence must hold for a whole bounded interval."""

    stable_interval = 2.0
    monkeypatch.setattr(process_module, "_REAP_TIMEOUT_S", stable_interval)
    monkeypatch.setattr(process_module, "_POLL_INTERVAL_S", 0.005)
    scope_name = f"measure-twice-{uuid.uuid4().hex}"
    relative = process_module._expected_scope_relative_path(scope_name)
    scope_path = Path(process_module._scope_collection_path(relative))
    assert not scope_path.exists()
    baseline = _linux_fd_snapshot()
    proc = subprocess.Popen(
        ("/bin/sleep", "30"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    started = time.monotonic()
    try:
        error = process_module._cleanup_failed_linux_start(proc, None, relative)
    finally:
        if proc.poll() is None:  # pragma: no cover - only on a production regression.
            proc.kill()
            proc.wait(timeout=5)
    elapsed = time.monotonic() - started

    assert error is None
    # The direct systemd-run owner is reaped before absence can be claimed at all.
    assert proc.poll() is not None
    # A first ENOENT is not proof: the whole bounded interval must elapse.
    assert elapsed >= stable_interval
    assert not scope_path.exists()
    _assert_linux_fd_baseline(baseline)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux no-handshake absence-interval invariant")
def test_linux_scope_absence_interval_restarts_when_the_path_reappears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An asynchronously reappearing scope restarts the bounded absence interval.

    The path must be ABSENT at entry, then REAPPEAR mid-interval, then go absent again.  A
    present-then-absent transition never reaches the reset at all: ``stable_absence_deadline``
    is still ``None`` when the path first goes absent, so deleting the reset line would leave
    such a test green.  Only a genuine reappearance discriminates it.
    """

    stable_interval = 1.0
    monkeypatch.setattr(process_module, "_REAP_TIMEOUT_S", stable_interval)
    monkeypatch.setattr(process_module, "_POLL_INTERVAL_S", 0.005)
    scope = tmp_path / "phantom.scope"
    assert not scope.exists()
    returned: list[float] = []
    errors: list[BaseException] = []

    def wait_for_collection() -> None:
        try:
            process_module._wait_for_scope_collection(str(scope), None)
            returned.append(time.monotonic())
        except BaseException as exc:  # preserve the production exception from the worker.
            errors.append(exc)

    worker = threading.Thread(target=wait_for_collection, daemon=True)
    started = time.monotonic()
    worker.start()
    # Let a partial absence interval accumulate, then make the scope reappear well inside it.
    time.sleep(stable_interval * 0.3)
    scope.mkdir()
    time.sleep(stable_interval * 0.2)
    removed_at = time.monotonic()
    scope.rmdir()
    worker.join(timeout=5 * stable_interval + 5)

    assert not worker.is_alive()
    assert not errors, errors
    assert len(returned) == 1
    # The interval restarts from the LAST absence.  Without the reset the worker would return at
    # roughly `started + stable_interval`, i.e. BEFORE `removed_at + stable_interval` -- the
    # partial interval banked before the reappearance would have counted toward the proof.
    assert returned[0] - removed_at >= stable_interval
    assert returned[0] - started >= stable_interval * 1.5


def _packed_supervisor_status(
    *,
    wait_status: int = 0,
    exec_error: int = 0,
    cpu_microseconds: int = 4_321_000,
    hard_limit: int = 0,
    hard_observed: int = 0,
    setup_error: int = 0,
) -> bytes:
    return process_module._SUPERVISOR_STATUS.pack(
        process_module._SUPERVISOR_MAGIC,
        wait_status,
        exec_error,
        cpu_microseconds,
        hard_limit,
        hard_observed,
        setup_error,
    )


@pytest.mark.skipif(sys.platform != "linux", reason="Linux fast scope-collection invariant")
@pytest.mark.parametrize(
    "case",
    ["collected", "owner-alive", "path-replaced", "malformed-control", "missing-supervisor-record"],
)
def test_linux_fast_scope_collection_is_terminal_only_with_proved_owner_exit_and_exact_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """systemd's --collect branch is terminal only once every precondition is proved."""

    monkeypatch.setattr(process_module, "_REAP_TIMEOUT_S", 0.5)
    monkeypatch.setattr(process_module, "_POLL_INTERVAL_S", 0.005)
    baseline = _linux_fd_snapshot()
    scope = tmp_path / "measure-twice-fake.scope"
    scope.mkdir()
    (scope / "cgroup.kill").write_bytes(b"")
    kill_fd = os.open(scope / "cgroup.kill", os.O_WRONLY | os.O_CLOEXEC)
    capability = LinuxPathCapability.acquire_absolute(scope, expected="directory")

    live_owner: subprocess.Popen[bytes] | None = None
    if case == "malformed-control":
        (scope / "cgroup.events").write_text("populated\n", encoding="utf-8")
    else:
        (scope / "cgroup.kill").unlink()
        scope.rmdir()
        if case == "path-replaced":
            scope.mkdir()

    if case == "owner-alive":
        live_owner = subprocess.Popen(
            ("/bin/sleep", "30"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        owner_identity = (
            live_owner.pid,
            cast("int", process_module._pid_starttime(live_owner.pid)),
        )
    else:
        reaped = subprocess.Popen(
            ("/bin/true",),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        reaped.wait(timeout=5)
        owner_identity = (reaped.pid, 1)

    guard = process_module._LinuxResourceGuardState(
        capability,
        kill_fd,
        LinuxResourceGuard(1, 1, 100),
        str(scope),
        capability.identity,
        {},
        {},
    )
    guard.outer_owner_identity = owner_identity
    guard.trusted_for_cleanup = True
    guard.validated_for_release = True

    status_read_fd, status_write_fd = os.pipe()
    if case != "missing-supervisor-record":
        os.write(status_write_fd, _packed_supervisor_status(hard_limit=1, hard_observed=987_654))
    os.close(status_write_fd)
    proc = subprocess.Popen(
        ("/bin/true",),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    proc.wait(timeout=5)
    runtime = process_module._RunningProcess(
        proc=proc,
        started=time.monotonic(),
        process_group_id=None,
        status_read_fd=status_read_fd,
    )
    runtime.resource_guard = guard

    expected_message = {
        "owner-alive": "could not read Linux resource guard cgroup.events",
        "path-replaced": "scope path was replaced before collection",
        "malformed-control": "cgroup.events is malformed",
        "missing-supervisor-record": "invalid status record",
    }.get(case)
    try:
        if expected_message is None:
            process_module._cleanup_process(runtime, abnormal=False)
            assert guard.collected is True
            status = runtime.target_status
            assert status is not None
            assert status.cpu_microseconds == 4_321_000
            assert (status.hard_limit, status.hard_observed) == (1, 987_654)
        else:
            with pytest.raises(ProcessExecutionError, match=expected_message):
                process_module._cleanup_process(runtime, abnormal=False)
            if case == "missing-supervisor-record":
                # Collection still completed; the record is a separate fail-closed gate.
                assert guard.collected is True
            elif case != "malformed-control":
                assert guard.collected is False
        assert runtime.resource_guard is None
    finally:
        if live_owner is not None:
            live_owner.kill()
            live_owner.wait(timeout=5)
        guard.close()
        process_module._close_fd(runtime.status_read_fd)
    _assert_linux_fd_baseline(baseline)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux terminal cgroup-emptiness invariant")
def test_linux_normal_exit_still_kills_a_populated_scope_and_proves_it_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A descendant outliving a cleanly-exited target is caught by the cgroup, not by /proc."""

    monkeypatch.setattr(process_module, "_REAP_TIMEOUT_S", 0.3)
    monkeypatch.setattr(process_module, "_POLL_INTERVAL_S", 0.005)
    baseline = _linux_fd_snapshot()
    scope = tmp_path / "populated.scope"
    scope.mkdir()
    (scope / "cgroup.events").write_text("populated 1\n", encoding="utf-8")
    (scope / "cgroup.kill").write_bytes(b"")
    kill_fd = os.open(scope / "cgroup.kill", os.O_WRONLY | os.O_CLOEXEC)
    capability = LinuxPathCapability.acquire_absolute(scope, expected="directory")
    guard = process_module._LinuxResourceGuardState(
        capability,
        kill_fd,
        LinuxResourceGuard(1, 1, 100),
        str(scope),
        capability.identity,
        {},
        {},
    )
    guard.outer_owner_identity = (os.getpid(), 1)
    guard.trusted_for_cleanup = True
    guard.validated_for_release = True

    status_read_fd, status_write_fd = os.pipe()
    os.write(status_write_fd, _packed_supervisor_status())
    os.close(status_write_fd)
    proc = subprocess.Popen(
        ("/bin/true",),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    proc.wait(timeout=5)
    runtime = process_module._RunningProcess(
        proc=proc,
        started=time.monotonic(),
        process_group_id=None,
        status_read_fd=status_read_fd,
    )
    runtime.resource_guard = guard
    assert runtime.tracker is None  # so `tracked` stays empty and only the late block can kill.
    try:
        # abnormal=False and an empty /proc snapshot: the cgroup is still tested and killed.
        with pytest.raises(ProcessExecutionError, match="scope remained populated"):
            process_module._cleanup_process(runtime, abnormal=False)
        assert (scope / "cgroup.kill").read_bytes() == b"1"
        assert runtime.resource_guard is None
    finally:
        guard.close()
        process_module._close_fd(runtime.status_read_fd)
    _assert_linux_fd_baseline(baseline)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux SCM_RIGHTS handshake invariant")
def test_supervisor_status_wire_shape_has_one_owner_and_survives_large_ceilings() -> None:
    """The supervisor and the parent read ONE wire shape, wide enough for real ceilings.

    ``hard_observed`` carries cgroup ``memory.peak`` and tmpfs byte capacity, both sourced from
    ``Ceilings`` values that are validated only as positive ints.  As a signed 32-bit field it
    overflowed above 2 GiB, and the terminal ``_write_status`` runs OUTSIDE the supervisor's
    guarded ``try``, so ``struct.error`` escaped, the supervisor died without writing a record,
    and a REAL hard cgroup event surfaced as an ``invalid status record`` infrastructure failure
    -- precisely when the guard fired.
    """

    # One owner: the format the supervisor packs with is substituted from the parent's constant.
    assert process_module._SUPERVISOR_STATUS_FORMAT in process_module._SUPERVISOR_CODE
    assert "__MT_STATUS_FORMAT__" not in process_module._SUPERVISOR_CODE
    assert "__MT_STATUS_MAGIC__" not in process_module._SUPERVISOR_CODE
    compile(process_module._SUPERVISOR_CODE, "<supervisor>", "exec")

    # A ceiling above the old signed-32-bit bound must round-trip rather than raise.
    oversized = 4 * 1024 * 1024 * 1024
    payload = process_module._SUPERVISOR_STATUS.pack(
        process_module._SUPERVISOR_MAGIC, 0, 0, 12_345, 1, oversized, 0
    )
    assert len(payload) == process_module._SUPERVISOR_STATUS.size
    unpacked = process_module._SUPERVISOR_STATUS.unpack(payload)
    assert unpacked[0] == process_module._SUPERVISOR_MAGIC
    assert unpacked[5] == oversized


def test_collected_cgroup_error_recognises_both_retired_scope_shapes() -> None:
    """A retired cgroup v2 scope fails in two shapes, and only those two may be accepted.

    ``open()`` raises ENOENT when the control file is already unlinked; when the open wins the
    race the following ``read()`` raises ENODEV on the now-removed cgroup.  Accepting only the
    first turned a healthy-but-slow systemd teardown into an evaluator-infrastructure failure.
    Accepting any ``OSError`` would be worse -- it would launder a permission or I/O fault into
    "the cgroup is gone", so the negative cases below are the load-bearing half of this test.
    """

    assert process_module._is_collected_cgroup_error(FileNotFoundError(errno.ENOENT, "gone"))
    assert process_module._is_collected_cgroup_error(OSError(errno.ENODEV, "no such device"))
    assert not process_module._is_collected_cgroup_error(OSError(errno.EACCES, "denied"))
    assert not process_module._is_collected_cgroup_error(OSError(errno.EIO, "io error"))
    assert not process_module._is_collected_cgroup_error(ValueError("malformed"))
    assert not process_module._is_collected_cgroup_error(None)


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


def _assert_path_stays_absent(path: Path, *, duration_s: float = 1.5) -> None:
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        assert not path.exists()
        time.sleep(0.02)


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


def _release_marker_code(ready: Path, marker: Path, release: Path) -> str:
    """Build a Linux child canary whose PID/start token can be resolved by its owner."""

    return (
        "import os,pathlib,time; "
        f"ready=pathlib.Path({str(ready)!r}); "
        f"marker=pathlib.Path({str(marker)!r}); "
        f"release=pathlib.Path({str(release)!r}); "
        "pid=os.getpid(); "
        "stat_record=pathlib.Path(f'/proc/{pid}/stat').read_text(encoding='ascii'); "
        "start_token=stat_record[stat_record.rfind(')')+2:].split()[19]; "
        "ready.write_text(f'{pid}:{start_token}',encoding='ascii'); "
        "\nwhile not release.exists(): time.sleep(.01)\n"
        "marker.write_text('escaped',encoding='utf-8')"
    )


def _ready_identity(path: Path) -> tuple[int, int] | None:
    try:
        namespace_pid, start_token = path.read_text(encoding="ascii").split(":", maxsplit=1)
        return (int(namespace_pid), int(start_token))
    except (OSError, ValueError):
        return None


def _nested_linux_pids(pid: int) -> tuple[int, ...]:
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="ascii")
    except (FileNotFoundError, OSError, UnicodeError):
        return ()
    for line in status.splitlines():
        if line.startswith("NSpid:"):
            try:
                return tuple(int(value) for value in line.split()[1:])
            except ValueError:
                return ()
    return ()


def _host_identity_for_live_child(
    owner_pid: int, namespace_pid: int, start_token: int
) -> tuple[int, int] | None:
    pending = [owner_pid]
    visited: set[int] = set()
    while pending:
        parent_pid = pending.pop()
        if parent_pid in visited:
            continue
        visited.add(parent_pid)
        for child_pid in process_module._posix_direct_children(parent_pid):
            if child_pid not in visited:
                pending.append(child_pid)
            if process_module._pid_starttime(child_pid) == start_token and _nested_linux_pids(
                child_pid
            )[-1:] == (namespace_pid,):
                return (child_pid, start_token)
    return None


def _wait_for_live_linux_identity(
    path: Path, *, owner_pid: int, timeout_s: float = 5.0
) -> tuple[int, int]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        identity = _ready_identity(path)
        if identity is not None:
            host_identity = _host_identity_for_live_child(owner_pid, *identity)
            if host_identity is not None:
                return host_identity
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for live child identity: {path}")


def _assert_linux_identity_gone(identity: tuple[int, int]) -> None:
    host_pid, start_token = identity
    assert process_module._pid_starttime(host_pid) != start_token, (
        f"child process identity survived cleanup: pid={host_pid}, starttime={start_token}"
    )


@pytest.mark.skipif(sys.platform != "linux", reason="Linux post-spawn cleanup invariant")
@pytest.mark.parametrize("failing_thread", ["agent-bench-descendants", "agent-bench-stderr"])
def test_linux_partial_thread_start_failure_reaps_process_and_closes_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_thread: str,
) -> None:
    baseline = _linux_fd_snapshot()
    ready = tmp_path / f"{failing_thread}-ready"
    marker = tmp_path / f"{failing_thread}-escaped"
    release = tmp_path / f"{failing_thread}-release"
    spawned: list[subprocess.Popen[bytes]] = []
    ready_identity: tuple[int, int] | None = None
    original_popen = process_module.subprocess.Popen
    original_start = threading.Thread.start

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        proc = original_popen(*args, **kwargs)  # type: ignore[arg-type]
        spawned.append(proc)
        return proc

    def start_then_fail(thread: threading.Thread) -> None:
        nonlocal ready_identity
        original_start(thread)
        if thread.name == failing_thread:
            ready_identity = _wait_for_live_linux_identity(ready, owner_pid=spawned[0].pid)
            raise RuntimeError(f"injected {failing_thread} start failure")

    monkeypatch.setattr(process_module.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(threading.Thread, "start", start_then_fail)
    with pytest.raises(RuntimeError, match="injected"):
        run_process(_request(tmp_path, _release_marker_code(ready, marker, release)))

    assert len(spawned) == 1
    assert spawned[0].poll() is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(spawned[0].pid, os.WNOHANG)
    assert ready_identity is not None
    _assert_linux_identity_gone(ready_identity)
    assert not marker.exists()
    release.write_text("release", encoding="utf-8")
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


@pytest.mark.skipif(sys.platform != "linux", reason="Linux PID namespace procfs invariant")
def test_linux_outer_pid_namespace_mounts_procfs_for_its_target(tmp_path: Path) -> None:
    result = run_process(
        _request(
            tmp_path,
            "import os,pathlib; "
            "assert pathlib.Path('/proc/self').samefile(f'/proc/{os.getpid()}'); "
            "print(os.getpid())",
        )
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip().isdigit()


# The detached descendant waits this long for its release marker.  The promptness bound in
# test_namespace_owner_cleans_descendant_when_direct_child_exits is derived from it: if
# cleanup ever blocks on the descendant instead of reaping it, the run takes this long.
_DESCENDANT_RELEASE_DEADLINE_S: float = 10.0


def _post_cleanup_write_code(leaked: Path, *, release: Path | None = None) -> str:
    if release is None:
        return f"time.sleep(1.0); open({str(leaked)!r},'w',encoding='utf-8').write('leaked')"
    return (
        f"release={str(release)!r}; deadline=time.monotonic()+{_DESCENDANT_RELEASE_DEADLINE_S}; "
        "\nwhile not os.path.exists(release) and time.monotonic()<deadline: time.sleep(.01)\n"
        f"if os.path.exists(release): "
        f"open({str(leaked)!r},'w',encoding='utf-8').write('leaked')"
    )


def _tree_code(ready: Path, leaked: Path, *, release: Path | None = None) -> str:
    child = (
        "import os,sys,time; "
        "os.setsid() if os.name == 'posix' else None; "
        "open(sys.argv[1], 'w', encoding='utf-8').write('ready'); "
        f"{_post_cleanup_write_code(leaked, release=release)}"
    )
    return (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-I','-c',{child!r},{str(ready)!r},{str(leaked)!r}]); "
        f"p={str(ready)!r}; "
        "\nwhile not __import__('os').path.exists(p): time.sleep(.01)\n"
        "time.sleep(30)"
    )


def _leader_exits_first_code(ready: Path, leaked: Path, *, release: Path | None = None) -> str:
    code = _tree_code(ready, leaked, release=release)
    return code.rsplit("time.sleep(30)", 1)[0] + "pass"


def _double_fork_code(
    ready: Path,
    leaked: Path,
    *,
    hold_leader: bool,
    release: Path | None = None,
) -> str:
    leader_tail = "time.sleep(30)" if hold_leader else "pass"
    child_tail = _post_cleanup_write_code(leaked, release=release).replace("\n", "\n ")
    return (
        "import os,time\n"
        "pid=os.fork()\n"
        "if pid == 0:\n"
        " os.setsid()\n"
        " if os.fork(): os._exit(0)\n"
        f" open({str(ready)!r},'w',encoding='utf-8').write('ready')\n"
        " null=os.open(os.devnull,os.O_RDWR)\n"
        " [os.dup2(null,fd) for fd in (0,1,2)]\n"
        f" {child_tail}\n"
        " os._exit(0)\n"
        f"marker={str(ready)!r}\n"
        "while not os.path.exists(marker): time.sleep(.01)\n"
        f"{leader_tail}\n"
    )


def test_timeout_kills_detached_descendant_before_it_can_escape(tmp_path: Path) -> None:
    fd_baseline = _linux_fd_snapshot() if sys.platform == "linux" else None
    ready = tmp_path / "ready"
    leaked = tmp_path / "leaked"
    release = tmp_path / "release"
    result = run_process(
        _request(
            tmp_path,
            _tree_code(ready, leaked, release=release),
            timeout_s=5,
            stream_limit_bytes=1024,
        )
    )

    assert result.termination == "timeout"
    assert result.exit_code is None
    assert ready.is_file()
    release.write_text("release", encoding="utf-8")
    _assert_path_stays_absent(leaked)
    if fd_baseline is not None:
        _assert_linux_fd_baseline(fd_baseline)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux PID namespace descendant invariant")
def test_namespace_owner_cleans_descendant_when_direct_child_exits(tmp_path: Path) -> None:
    ready = tmp_path / "orphan-ready"
    leaked = tmp_path / "orphan-leaked"
    release = tmp_path / "orphan-release"
    started = time.monotonic()
    result = run_process(
        _request(
            tmp_path,
            _leader_exits_first_code(ready, leaked, release=release),
            timeout_s=5,
            stream_limit_bytes=1024,
        )
    )
    elapsed = time.monotonic() - started

    assert result.termination == "exited"
    assert result.exit_code == 0
    assert ready.is_file()
    # Promptness, not just correctness: the namespace owner REAPS the detached descendant rather
    # than waiting on it.  The descendant blocks for _DESCENDANT_RELEASE_DEADLINE_S and the
    # release marker is only written below, so a cleanup path that waits on it instead of
    # reaping it returns at ~that deadline.  Without this bound such a regression would only
    # make the test slower -- every other assertion here still passes once the descendant's
    # own deadline elapses, and no pytest-timeout is configured.
    assert elapsed < _DESCENDANT_RELEASE_DEADLINE_S / 2, (
        f"cleanup appears to block on the detached descendant: {elapsed:.2f}s"
    )
    release.write_text("release", encoding="utf-8")
    _assert_path_stays_absent(leaked)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux cleanup status-pipe invariant")
def test_linux_cleanup_does_not_block_on_status_pipe_when_owner_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _UnreapableOuter:
        stdin = None
        stdout = None
        stderr = None
        pid = 1

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired("unshare", timeout)

    read_fd, write_fd = os.pipe()
    runtime = process_module._RunningProcess(
        proc=cast("subprocess.Popen[bytes]", _UnreapableOuter()),
        started=time.monotonic(),
        process_group_id=1,
        status_read_fd=read_fd,
    )
    monkeypatch.setattr(process_module, "_kill_tree", lambda *_args: None)
    errors: list[BaseException] = []
    completed = threading.Event()

    def clean_up() -> None:
        try:
            process_module._cleanup_process(runtime, abnormal=True)
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()

    worker = threading.Thread(target=clean_up, daemon=True)
    worker.start()
    blocked = not completed.wait(0.5)
    try:
        if blocked:
            os.close(write_fd)
            write_fd = -1
        worker.join(timeout=2)
    finally:
        if write_fd >= 0:
            os.close(write_fd)

    assert not blocked
    assert completed.is_set()
    assert len(errors) == 1
    assert "direct child survived whole-tree termination" in str(errors[0])


@pytest.mark.skipif(sys.platform != "linux", reason="Linux PID namespace descendant invariant")
def test_descendant_owner_structurally_contains_every_reparented_session(
    tmp_path: Path,
) -> None:
    for index in range(3):
        direct_ready = tmp_path / f"direct-ready-{index}"
        direct_leaked = tmp_path / f"direct-leaked-{index}"
        forked_ready = tmp_path / f"forked-ready-{index}"
        forked_leaked = tmp_path / f"forked-leaked-{index}"
        release = tmp_path / f"release-{index}"
        detach = (
            "import os,time; os.setsid(); "
            f"open({str(direct_ready)!r},'w',encoding='utf-8').write('ready'); "
            "null=os.open(os.devnull,os.O_RDWR); "
            "[os.dup2(null,fd) for fd in (0,1,2)]; "
            f"{_post_cleanup_write_code(direct_leaked, release=release)}"
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
            f"{_post_cleanup_write_code(forked_leaked, release=release)}"
        )
        leader = (
            "import os,subprocess,sys,time; "
            f"subprocess.Popen([sys.executable,'-I','-c',{detach!r}]); "
            f"subprocess.Popen([sys.executable,'-I','-c',{double_fork!r}]); "
            f"paths=({str(direct_ready)!r},{str(forked_ready)!r}); "
            "\nwhile not all(os.path.exists(path) for path in paths): time.sleep(.01)\n"
        )
        result = run_process(_request(tmp_path, leader, timeout_s=10, stream_limit_bytes=1024))
        assert result.termination == "exited"
        assert result.exit_code == 0
        assert direct_ready.is_file()
        assert forked_ready.is_file()
        release.write_text("release", encoding="utf-8")
        _assert_path_stays_absent(direct_leaked)
        _assert_path_stays_absent(forked_leaked)


@pytest.mark.skipif(sys.platform != "linux", reason="double-fork requires Linux")
def test_timeout_kills_double_fork_descendant(tmp_path: Path) -> None:
    ready = tmp_path / "double-fork-timeout-ready"
    leaked = tmp_path / "double-fork-timeout-leaked"
    release = tmp_path / "double-fork-timeout-release"

    result = run_process(
        _request(
            tmp_path,
            _double_fork_code(ready, leaked, hold_leader=True, release=release),
            timeout_s=5,
            stream_limit_bytes=1024,
        )
    )

    assert result.termination == "timeout"
    assert ready.is_file()
    release.write_text("release", encoding="utf-8")
    _assert_path_stays_absent(leaked)


@pytest.mark.skipif(sys.platform != "linux", reason="double-fork requires Linux")
def test_keyboard_interrupt_kills_double_fork_tree_then_propagates(tmp_path: Path) -> None:
    fd_baseline = _linux_fd_snapshot() if sys.platform == "linux" else None
    ready = tmp_path / "interrupt-ready"
    leaked = tmp_path / "interrupt-leaked"
    release = tmp_path / "interrupt-release"
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
                    _double_fork_code(ready, leaked, hold_leader=True, release=release),
                    timeout_s=10,
                    stream_limit_bytes=1024,
                )
            )
    finally:
        trigger.join(timeout=6)

    assert interrupt_sent.is_set()
    assert ready.is_file()
    release.write_text("release", encoding="utf-8")
    _assert_path_stays_absent(leaked)
    if fd_baseline is not None:
        _assert_linux_fd_baseline(fd_baseline)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux monitor fail-closed invariant")
def test_descendant_monitor_exception_fails_closed_and_kills_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = _linux_fd_snapshot()
    ready = tmp_path / "monitor-failure-ready"
    marker = tmp_path / "monitor-failure-escaped"
    release = tmp_path / "monitor-failure-release"
    (tmp_path / "scan-me").write_text("seed", encoding="utf-8")
    ready_identity: tuple[int, int] | None = None
    spawned: list[subprocess.Popen[bytes]] = []
    original_popen = process_module.subprocess.Popen

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        proc = original_popen(*args, **kwargs)  # type: ignore[arg-type]
        spawned.append(proc)
        return proc

    def fail_tree_scan(_relative: str) -> None:
        nonlocal ready_identity
        ready_identity = _wait_for_live_linux_identity(ready, owner_pid=spawned[0].pid)
        raise RuntimeError("injected live monitor failure")

    request = ProcessRequest.create(
        argv=(PYTHON, "-I", "-c", _release_marker_code(ready, marker, release)),
        stdin="",
        cwd=tmp_path.resolve(),
        environment={},
        timeout_s=5,
        stream_limit_bytes=1024,
        resource_limits=ProcessResourceLimits(tree_files=10, tree_bytes=1024),
        tree_root=tmp_path.resolve(),
        _tree_before_open=fail_tree_scan,
    )

    monkeypatch.setattr(process_module.subprocess, "Popen", recording_popen)
    with pytest.raises(ProcessExecutionError, match="Linux descendant monitor failed"):
        run_process(request)

    assert len(spawned) == 1
    assert ready_identity is not None
    _assert_linux_identity_gone(ready_identity)
    assert not marker.exists()
    release.write_text("release", encoding="utf-8")
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
    # The unguarded process seam has no cgroup: every one of its ceilings is /proc sampling
    # and may never claim hard-guard.  This is what makes the evaluator seam's hard-guard
    # assertions non-tautological -- collapsing provenance to one constant reddens one side.
    assert result.resource_limit_provenance == "sampled-threshold"
    configured = {
        "cpu": limits.cpu_seconds,
        "memory": limits.memory_bytes,
        "processes": limits.processes,
    }[resource_name]
    observed = result.resource_limit_observed
    assert configured is not None and observed is not None
    assert result.resource_limit_value == configured
    # cpu crosses at >=; the sampled memory/process ceilings cross strictly above.
    assert observed >= configured if resource_name == "cpu" else observed > configured
    assert result.elapsed_ms < 4_000
    _assert_linux_fd_baseline(fd_baseline)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux live evaluator-tree monitor")
@pytest.mark.parametrize(
    ("count", "size", "files", "tree_bytes", "resource_name", "expected_observed"),
    # walk_tree raises on the first crossing: file-count at files + 1, file-bytes at the
    # first cumulative sum strictly greater than tree_bytes (50 + 50 + 50 = 150 > 100).
    [(6, 10, 5, 1_000, "file-count", 6), (3, 50, 10, 100, "file-bytes", 150)],
)
def test_linux_resource_monitor_terminates_live_tree_ceiling_crossing(
    tmp_path: Path,
    count: int,
    size: int,
    files: int,
    tree_bytes: int,
    resource_name: str,
    expected_observed: int,
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
    assert result.resource_limit_provenance == "sampled-threshold"
    assert result.resource_limit_value == (files if resource_name == "file-count" else tree_bytes)
    assert result.resource_limit_observed == expected_observed
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
