from __future__ import annotations

import copy
import errno
import itertools
import json
import os
import pickle
import select
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, cast

import pytest

import measure_twice.agent_bench.isolation as isolation_module
import measure_twice.agent_bench.process as process_module
from measure_twice.agent_bench._linux_capabilities import (
    LinuxCapabilityError,
    LinuxPathCapability,
    LinuxTreeDriftError,
    LinuxTreeLimitError,
    LinuxTreePolicyError,
    walk_tree,
)
from measure_twice.agent_bench.isolation import (
    CAPTURE_GIT_ENVIRONMENT,
    EVALUATOR_ENVIRONMENT,
    IsolationContractError,
    IsolationUnavailableError,
    LinuxIsolationPreflight,
    ResourceCeilingError,
    SandboxLaunch,
    allowlisted_environment,
    build_agent_sandbox,
    build_capture_sandbox,
    build_evaluator_sandbox,
    capture_git_add_argv,
    capture_git_diff_argv,
    enforce_evaluator_tree_ceiling,
    measure_tree_usage,
    preflight_linux_isolation,
    resolve_bubblewrap,
    secret_environment_values,
)
from measure_twice.agent_bench.models import (
    EVALUATOR_DIRECTORY_ALLOWANCE,
    Ceilings,
    evaluator_tmpfs_minimum_bytes,
    load_execution_profile,
)
from measure_twice.agent_bench.process import (
    EVALUATOR_WORKSPACE_FD_TOKEN,
    EvaluatorScratch,
    ModelTreeViolationError,
    ProcessExecutionError,
    ProcessRequest,
    ProcessResourceLimits,
    run_process,
)

ROOT = Path(__file__).resolve().parents[2]
EXECUTION_PROFILE = ROOT / "profiles" / "agent-execution-v1.json"
FIXTURES = Path(__file__).parent / "fixtures" / "isolation"
SANDBOX_HOME = "/tmp/home"  # noqa: S108 - private sandbox path asserted by the canary.
# Long enough that a release-then-verify inversion is caught with a wide margin: a released
# target reaches os.write in tens of milliseconds against a 1000 ms dwell polled every 20 ms.
_BARRIER_DWELL_S = 1.0
# Long enough to dominate poll granularity and interpreter start, short enough that
# it costs the gate half a second.
_TARGET_SLEEP_S = 0.5
_FAKE_ROOT = PurePosixPath("/var/lib/measure-twice-test")


def _ceilings(
    *,
    # Generous by default.  cgroup CPU accounting covers the WHOLE scope -- the namespace
    # supervisor and Bubblewrap as well as the target -- so three interpreter startups on a
    # contended host can cross a tight ceiling and stop a test that was measuring something else
    # entirely (observed: the RLIMIT_NOFILE probe killed at cpu=2 with observed=2,
    # provenance="sampled-threshold").  A test that MEANS to hit a ceiling sets it explicitly;
    # every other test must be unable to trip one by accident.
    cpu: int = 30,
    memory: int = 256 * 1024 * 1024,
    processes: int = 16,
    files: int = 100,
    file_bytes: int = 1024 * 1024,
    cpu_bandwidth_percent: int = 100,
    tmpfs_bytes: int | None = None,
    tmpfs_inodes: int | None = None,
) -> Ceilings:
    # Fourth copy of the byte floor removed: the helper is the one owner, so a page size
    # other than 4096 can no longer make these ceilings load-valid but launch-invalid.
    bounded_tmpfs_bytes = max(
        evaluator_tmpfs_minimum_bytes(file_bytes=file_bytes, files=files),
        2 * 1024 * 1024,
    )
    return Ceilings(
        changed_paths=100,
        patch_bytes=5 * 1024 * 1024,
        stream_bytes_each=10 * 1024 * 1024,
        cell_artifact_bytes=25 * 1024 * 1024,
        evaluator_cpu_s=cpu,
        evaluator_memory_bytes=memory,
        evaluator_processes=processes,
        evaluator_files=files,
        evaluator_file_bytes=file_bytes,
        evaluator_cpu_bandwidth_percent=cpu_bandwidth_percent,
        evaluator_tmpfs_bytes=(bounded_tmpfs_bytes if tmpfs_bytes is None else tmpfs_bytes),
        evaluator_tmpfs_inodes=(
            files + EVALUATOR_DIRECTORY_ALLOWANCE if tmpfs_inodes is None else tmpfs_inodes
        ),
    )


def _setenv(argv: tuple[str, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for index, argument in enumerate(argv):
        if argument == "--setenv":
            result[argv[index + 1]] = argv[index + 2]
    return result


class _FakeCapability:
    _fds = itertools.count(100)
    _identities: ClassVar[dict[str, int]] = {}
    instances: ClassVar[list[_FakeCapability]] = []

    def __init__(
        self,
        display_path: str,
        *,
        directory: bool = True,
        identity: tuple[int, int] | None = None,
    ) -> None:
        normalized = PurePosixPath(display_path).as_posix()
        self.display_path = normalized
        self.fd = next(self._fds)
        inode = self._identities.setdefault(normalized, len(self._identities) + 10)
        self.identity = (1, inode) if identity is None else identity
        self.st_dev, self.st_ino = self.identity
        self.st_mode = (stat.S_IFDIR | 0o700) if directory else (stat.S_IFREG | 0o700)
        self.filesystem_name = "ext4"
        self.closed = False
        self.fail_duplicate = False
        self.fail_reopen = False
        self.instances.append(self)

    def duplicate(self) -> _FakeCapability:
        if self.fail_duplicate:
            raise LinuxCapabilityError("injected duplicate failure")
        return type(self)(
            self.display_path,
            directory=stat.S_ISDIR(self.st_mode),
            identity=self.identity,
        )

    def reopen_directory(self) -> _FakeCapability:
        if self.fail_reopen:
            raise LinuxCapabilityError("injected reopen failure")
        return type(self)(self.display_path, identity=self.identity)

    def open_parent(self) -> _FakeCapability:
        return type(self)(PurePosixPath(self.display_path).parent.as_posix())

    def close(self) -> None:
        self.closed = True


class _FakePreflight:
    def __init__(self) -> None:
        self._bwrap = _FakeCapability("/usr/local/bin/bwrap", directory=False)
        self._runtime = (("/usr", _FakeCapability("/usr")),)
        self._network = (("/etc/hosts", _FakeCapability("/etc/hosts", directory=False)),)

    def acquire(self, value: str | os.PathLike[str], *, label: str) -> _FakeCapability:
        raw = os.fspath(value)
        if not isinstance(raw, str):
            raise IsolationContractError(f"{label} must be an absolute normalized Linux path")
        path = PurePosixPath(raw)
        components = raw.split("/")[1:]
        if (
            path.as_posix() != raw
            or not path.is_absolute()
            or any(component in {"", ".", ".."} for component in components)
        ):
            raise IsolationContractError(f"{label} must be an absolute normalized Linux path")
        try:
            path.relative_to(_FAKE_ROOT)
        except ValueError as exc:
            raise IsolationContractError(f"{label} is outside every preflighted ext4 root") from exc
        return _FakeCapability(raw)

    def duplicate_bwrap(self) -> _FakeCapability:
        return self._bwrap.duplicate()

    def duplicate_runtime(self) -> tuple[tuple[str, _FakeCapability], ...]:
        return tuple(
            (destination, capability.duplicate()) for destination, capability in self._runtime
        )

    def duplicate_network(self) -> tuple[tuple[str, _FakeCapability], ...]:
        return tuple(
            (destination, capability.duplicate()) for destination, capability in self._network
        )


def _fake_preflight() -> LinuxIsolationPreflight:
    return cast("LinuxIsolationPreflight", _FakePreflight())


def _request_from_launch(launch: SandboxLaunch) -> ProcessRequest:
    return launch.process_request(stdin="", timeout_s=2, stream_limit_bytes=64 * 1024)


def _fake_capture_children(
    _root: object,
    *,
    omit_names: frozenset[str],
    before_open: Callable[[str], None] | None,
) -> tuple[tuple[str, Any], ...]:
    assert omit_names == frozenset({".git"})
    if before_open is not None:
        before_open("visible.txt")
    return (("visible.txt", _FakeCapability("/submitted/visible.txt", directory=False)),)


def test_environment_is_exactly_allowlisted_and_secret_names_fail_closed() -> None:
    source = {
        "LANG": "host-locale",
        "TZ": "UTC",
        "UNRELATED": "not-forwarded",
        "OPENAI_API_KEY": "credential-value",
    }
    clean = allowlisted_environment(
        source,
        allowed_names=("LANG", "TZ"),
        fixed={"LANG": "C.UTF-8", "HOME": "/sandbox-home"},
    )
    assert clean == (("HOME", "/sandbox-home"), ("LANG", "C.UTF-8"), ("TZ", "UTC"))
    assert "credential-value" not in repr(clean)
    with pytest.raises(IsolationContractError, match="secret environment name") as exc_info:
        allowlisted_environment(source, allowed_names=("OPENAI_API_KEY",))
    assert "credential-value" not in str(exc_info.value)
    with pytest.raises(IsolationContractError, match="secret environment name"):
        allowlisted_environment(
            {"AWS_ACCESS_KEY_ID": "not-a-real-access-key"},
            allowed_names=("AWS_ACCESS_KEY_ID",),
        )


def test_secret_environment_values_are_deduplicated_without_names_or_empty_values() -> None:
    source = {
        "OPENAI_API_KEY": "same-value",
        "CLAUDE_TOKEN": "same-value",
        "PASSWORD": "",
        "PATH": "same-value",
    }
    assert secret_environment_values(source) == ("same-value",)
    with pytest.raises(IsolationContractError, match="UTF-8 encodable"):
        secret_environment_values({"ACCESS_TOKEN": "\ud800"})


def test_exported_fixed_environments_are_immutable() -> None:
    with pytest.raises(TypeError):
        cast("dict[str, str]", CAPTURE_GIT_ENVIRONMENT)["MUTATED"] = "1"
    with pytest.raises(TypeError):
        cast("dict[str, str]", EVALUATOR_ENVIRONMENT)["MUTATED"] = "1"


def test_agent_builder_renders_only_fd_sources_and_scrubs_secrets() -> None:
    credential = "not-a-real-credential"
    launch = build_agent_sandbox(
        _fake_preflight(),
        workspace=f"{_FAKE_ROOT}/agent-workspace",
        command=("/usr/bin/codex", "exec"),
        source_environment={"TZ": "UTC", "OPENAI_API_KEY": credential},
    )
    request = _request_from_launch(launch)
    try:
        rendered = "\0".join(request.argv)
        assert launch.profile == "agent"
        assert launch.network_isolated is False
        assert "--share-net" in request.argv
        assert launch.writable_mounts == ("/workspace",)
        assert _setenv(request.argv)["TZ"] == "UTC"
        assert credential not in rendered
        assert str(_FAKE_ROOT) not in rendered
        assert "--bind" not in request.argv
        assert "--ro-bind" not in request.argv
        assert "--bind-fd" in request.argv
        assert "--ro-bind-fd" in request.argv
        assert request.argv[0].startswith("/proc/self/fd/")
        for absent in ("/suite", "/oracle", "/run-store", "/home/operator", "/mnt/c"):
            assert absent not in rendered
    finally:
        request.close()
        launch.close()

    with pytest.raises(IsolationContractError, match="passthrough is restricted"):
        build_agent_sandbox(
            _fake_preflight(),
            workspace=f"{_FAKE_ROOT}/agent-workspace",
            command=("/usr/bin/true",),
            source_environment={"AWS_PROFILE": "credential-profile"},
            allowed_environment_names=("AWS_PROFILE",),
        )


def test_capture_builder_omits_git_and_uses_trusted_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(isolation_module, "open_verified_children", _fake_capture_children)
    launch = build_capture_sandbox(
        _fake_preflight(),
        submitted_tree=f"{_FAKE_ROOT}/submitted",
        capture_repository=f"{_FAKE_ROOT}/repository",
        command=("/usr/bin/git", "status"),
    )
    request = _request_from_launch(launch)
    try:
        rendered = "\0".join(request.argv)
        assert launch.network_isolated is True
        assert "--share-net" not in request.argv
        assert "/submitted/visible.txt" in launch.read_only_mounts
        assert "/submitted/.git" not in rendered
        assert _setenv(request.argv) == dict(CAPTURE_GIT_ENVIRONMENT)
        assert str(_FAKE_ROOT) not in rendered
    finally:
        request.close()
        launch.close()


def test_evaluator_builder_is_separate_no_network_profile_with_all_ceilings() -> None:
    profile = load_execution_profile(EXECUTION_PROFILE)
    launch = build_evaluator_sandbox(
        _fake_preflight(),
        workspace=f"{_FAKE_ROOT}/evaluator-workspace",
        oracle=f"{_FAKE_ROOT}/evaluator-oracle",
        runtime=f"{_FAKE_ROOT}/evaluator-runtime",
        command=("/opt/measure-twice/runtime/python3.12", "-B"),
        ceilings=profile.ceilings,
    )
    request = _request_from_launch(launch)
    try:
        assert launch.profile == "evaluator"
        assert launch.network_isolated is True
        assert "--share-net" not in request.argv
        assert launch.writable_mounts == ("/workspace",)
        assert "/opt/measure-twice/oracle" in launch.read_only_mounts
        assert "/opt/measure-twice/runtime" in launch.read_only_mounts
        assert launch.resource_limits is not None
        assert launch.resource_limits.cpu_seconds == profile.ceilings.evaluator_cpu_s
        assert launch.resource_limits.memory_bytes == profile.ceilings.evaluator_memory_bytes
        assert launch.resource_limits.processes == profile.ceilings.evaluator_processes
        assert launch.resource_limits.file_bytes == profile.ceilings.evaluator_file_bytes
        assert launch.resource_limits.tree_files == profile.ceilings.evaluator_files
        assert launch.resource_limits.tree_bytes == profile.ceilings.evaluator_file_bytes
        assert launch.evaluator_file_limit == profile.ceilings.evaluator_files
        assert launch.evaluator_bytes_limit == profile.ceilings.evaluator_file_bytes
        assert _setenv(request.argv) == dict(EVALUATOR_ENVIRONMENT)
        assert str(_FAKE_ROOT) not in "\0".join(request.argv)
    finally:
        request.close()
        launch.close()


def test_builders_reject_unpreflighted_or_overlapping_mount_sources() -> None:
    preflight = _fake_preflight()
    with pytest.raises(IsolationContractError, match="outside every preflighted"):
        build_agent_sandbox(
            preflight,
            workspace="/home/operator/agent-workspace",
            command=("/usr/bin/true",),
        )
    with pytest.raises(IsolationContractError, match="may not overlap"):
        build_capture_sandbox(
            preflight,
            submitted_tree=f"{_FAKE_ROOT}/submitted",
            capture_repository=f"{_FAKE_ROOT}/submitted/repository",
            command=("/usr/bin/true",),
        )
    with pytest.raises(IsolationContractError, match="may not overlap"):
        build_evaluator_sandbox(
            preflight,
            workspace=f"{_FAKE_ROOT}/workspace",
            oracle=f"{_FAKE_ROOT}/workspace/oracle",
            runtime=f"{_FAKE_ROOT}/runtime",
            command=("/usr/bin/true",),
            ceilings=_ceilings(),
        )


@pytest.mark.parametrize(
    "bad_path",
    [
        f"{_FAKE_ROOT}/workspace/",
        f"{_FAKE_ROOT}//workspace",
        f"{_FAKE_ROOT}/./workspace",
        f"{_FAKE_ROOT}/workspace/../escape",
    ],
)
def test_every_caller_root_rejects_non_normalized_spelling(bad_path: str) -> None:
    with pytest.raises(IsolationContractError, match="normalized"):
        build_agent_sandbox(
            _fake_preflight(),
            workspace=bad_path,
            command=("/usr/bin/true",),
        )


def test_capture_git_argv_and_environment_are_locked() -> None:
    add = capture_git_add_argv()
    diff = capture_git_diff_argv()
    assert add[-5:] == ("add", "-A", "-f", "--", ".")
    assert diff[-6:] == (
        "diff",
        "--cached",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-textconv",
    )
    for argv in (add, diff):
        assert argv[0] == "/usr/bin/git"
        assert "core.hooksPath=/dev/null" in argv
        assert "core.attributesFile=/dev/null" in argv
        assert "core.fsmonitor=false" in argv
    assert CAPTURE_GIT_ENVIRONMENT["GIT_CONFIG_NOSYSTEM"] == "1"
    assert CAPTURE_GIT_ENVIRONMENT["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert CAPTURE_GIT_ENVIRONMENT["GIT_ATTR_NOSYSTEM"] == "1"
    with pytest.raises(IsolationContractError, match="pinned /usr runtime"):
        capture_git_add_argv("/workspace/git")


def test_wsl_preflight_rejects_mnt_before_touching_task_bytes() -> None:
    with pytest.raises(IsolationUnavailableError, match=r"/mnt/\*"):
        preflight_linux_isolation([Path("/mnt/c/untrusted")])
    with pytest.raises(IsolationUnavailableError, match="at least one"):
        preflight_linux_isolation([])


def test_unavailable_bubblewrap_fails_with_one_stable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> LinuxPathCapability:
        raise LinuxCapabilityError("candidate absent")

    monkeypatch.setattr(LinuxPathCapability, "acquire_absolute", unavailable)
    with pytest.raises(IsolationUnavailableError) as exc_info:
        resolve_bubblewrap()
    assert str(exc_info.value) == (
        "compatible Bubblewrap with behavioral --bind-fd/--ro-bind-fd support is unavailable"
    )


def test_launch_and_request_are_one_shot_and_close_every_owned_capability() -> None:
    launch = build_agent_sandbox(
        _fake_preflight(),
        workspace=f"{_FAKE_ROOT}/workspace",
        command=("/usr/bin/true",),
    )
    launch_owned = launch._launch_capabilities()
    request = _request_from_launch(launch)
    request_owned = request._all_capabilities()
    assert all(cast("_FakeCapability", capability).closed for capability in launch_owned)
    with pytest.raises(IsolationContractError, match="one-shot"):
        _request_from_launch(launch)
    request.close()
    assert all(cast("_FakeCapability", capability).closed for capability in request_owned)
    launch.close()


def test_process_request_partial_duplicate_failure_closes_originals_and_duplicates() -> None:
    _FakeCapability.instances.clear()
    launch = build_agent_sandbox(
        _fake_preflight(),
        workspace=f"{_FAKE_ROOT}/workspace",
        command=("/usr/bin/true",),
    )
    originals = launch._launch_capabilities()
    cast("_FakeCapability", launch._mounts[-1].capability).fail_duplicate = True
    before = len(_FakeCapability.instances)
    with pytest.raises(LinuxCapabilityError, match="injected duplicate"):
        _request_from_launch(launch)
    assert all(cast("_FakeCapability", capability).closed for capability in originals)
    assert all(capability.closed for capability in _FakeCapability.instances[before:])


def test_new_launch_partial_failure_rolls_back_bwrap_and_mounts() -> None:
    preflight = cast("_FakePreflight", _fake_preflight())
    workspace = preflight.acquire(f"{_FAKE_ROOT}/workspace", label="workspace")
    workspace.fail_reopen = True
    mounts = isolation_module._base_mounts(
        cast("LinuxIsolationPreflight", preflight), include_network=True
    )
    created_before = len(_FakeCapability.instances)
    with pytest.raises(LinuxCapabilityError, match="injected reopen"):
        isolation_module._new_launch(
            profile="agent",
            preflight=cast("LinuxIsolationPreflight", preflight),
            network_isolated=False,
            command=("/usr/bin/true",),
            environment=(),
            mounts=mounts,
            cwd_source=cast("LinuxPathCapability", workspace),
        )
    assert all(cast("_FakeCapability", mount.capability).closed for mount in mounts)
    assert all(capability.closed for capability in _FakeCapability.instances[created_before:])


def _is_wsl2() -> bool:
    if sys.platform != "linux":
        return False
    try:
        kernel = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8").lower()
    except OSError:
        return False
    return "microsoft" in kernel and "wsl2" in kernel


def _linux_capability(path: str | Path, *, expected: str = "directory") -> LinuxPathCapability:
    if sys.platform != "linux":
        pytest.skip("Linux descriptor capabilities require Linux")
    return LinuxPathCapability.acquire_absolute(path, expected=cast("Any", expected))


def _manual_preflight(tmp_path: Path) -> LinuxIsolationPreflight:
    root = _linux_capability(tmp_path)
    if root.filesystem_name != "ext4":
        root.close()
        pytest.skip("descriptor race canaries require an ext4 temporary root")
    bwrap = LinuxPathCapability.acquire_absolute(
        "/usr/bin/true",
        expected="regular",
        allow_symlinks=True,
        executable=True,
    )
    runtime = LinuxPathCapability.acquire_absolute("/usr", expected="directory")
    return LinuxIsolationPreflight(
        bwrap_version="construction-only",
        distribution="ubuntu",
        distribution_version="24.04",
        kernel_version="test",
        _bwrap=bwrap,
        _runtime=(("/usr", runtime),),
        _network=(),
        _roots=(root,),
    )


def _real_preflight(tmp_path: Path) -> LinuxIsolationPreflight:
    if not _is_wsl2():
        pytest.skip("accepted live substrate is WSL2 Ubuntu 24.04")
    try:
        return preflight_linux_isolation([tmp_path])
    except IsolationUnavailableError as exc:
        pytest.fail(f"WSL2 isolation preflight failed closed: {exc}")


@pytest.mark.linux_isolation
def test_wsl_absolute_resolver_target_is_fd_pinned_before_bubblewrap() -> None:
    if not _is_wsl2():
        pytest.skip("accepted resolver target requires WSL2 Ubuntu 24.04")
    entries = isolation_module._acquire_trusted_network_sources()
    try:
        resolver = dict(entries)["/etc/resolv.conf"]
        assert stat.S_ISREG(resolver.st_mode)
        assert os.pread(resolver.fd, 1, 0)
        if Path("/etc/resolv.conf").is_symlink():
            assert os.readlink("/etc/resolv.conf") == "/mnt/wsl/resolv.conf"
            assert resolver.display_path == "/mnt/wsl/resolv.conf"
    finally:
        for _destination, capability in entries:
            capability.close()


def _copy_fixture(runtime: Path, name: str) -> None:
    runtime.mkdir(exist_ok=True)
    shutil.copyfile(FIXTURES / name, runtime / name)


def _run_launch(
    launch: SandboxLaunch,
    *,
    stdin: str = "",
    timeout: float = 5,
    stream_limit: int = 64 * 1024,
):
    request = launch.process_request(
        stdin=stdin,
        timeout_s=timeout,
        stream_limit_bytes=stream_limit,
    )
    return run_process(request)


def _host_private_mountpoint_identity() -> tuple[int, int]:
    if sys.platform != "linux":
        pytest.skip("host private-mountpoint canary requires Linux")
    metadata = os.stat("/var/tmp")  # noqa: S108 - inspected as a host-mount isolation canary.
    return (metadata.st_dev, metadata.st_ino)


def _host_private_mountpoint_capability() -> LinuxPathCapability:
    return _linux_capability("/var/tmp")  # noqa: S108 - isolation canary.


def _assert_path_stays_absent(path: Path, *, duration_s: float = 1.5) -> None:
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        assert not path.exists()
        time.sleep(0.02)


class _RaceBarrier:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.released = threading.Event()
        self._arrivals: dict[str, int] = {}
        self._arrival_lock = threading.Lock()

    def hook(self, expected: str, *, ordinal: int = 1) -> Callable[[str], None]:
        """Block at the production seam on the ``ordinal``-th arrival of ``expected``.

        The same ``before_open`` seam is shared by the live monitor scan and the terminal
        post-quiescence scan, and they run on different threads.  Counting arrivals is what
        lets a test race one specific scan instead of whichever one happens to arrive first.
        """

        def wait_at_seam(label: str) -> None:
            if label != expected:
                return
            with self._arrival_lock:
                self._arrivals[label] = self._arrivals.get(label, 0) + 1
                if self._arrivals[label] != ordinal:
                    return
            self.entered.set()
            assert self.released.wait(10), "test did not release production race hook"

        return wait_at_seam

    def run(
        self,
        operation: Callable[[], Any],
        mutate: Callable[[], None],
        *,
        enter_timeout_s: float = 5.0,
        join_timeout_s: float = 5.0,
    ) -> tuple[Any | None, BaseException | None]:
        outcome: list[Any] = []
        errors: list[BaseException] = []

        def wrapped() -> None:
            try:
                outcome.append(operation())
            except BaseException as exc:  # preserve the production exception from the worker.
                errors.append(exc)

        thread = threading.Thread(target=wrapped, daemon=True)
        thread.start()
        assert self.entered.wait(enter_timeout_s), "production race hook was not reached"
        mutate()
        self.released.set()
        thread.join(join_timeout_s)
        assert not thread.is_alive(), "production operation did not leave the race barrier"
        return (outcome[0] if outcome else None, errors[0] if errors else None)


@pytest.mark.linux_isolation
def test_linux_capability_is_noncopyable_nonserializable_and_idempotently_closed(
    tmp_path: Path,
) -> None:
    capability = _linux_capability(tmp_path)
    raw_fd = capability.fd
    duplicate = capability.duplicate()
    with pytest.raises(TypeError):
        copy.copy(capability)
    with pytest.raises(TypeError):
        copy.deepcopy(capability)
    with pytest.raises(TypeError):
        pickle.dumps(capability)
    capability.close()
    capability.close()
    with pytest.raises(OSError) as exc_info:
        os.fstat(raw_fd)
    assert exc_info.value.errno == errno.EBADF
    assert os.fstat(duplicate.fd).st_ino == duplicate.st_ino
    duplicate.close()


@pytest.mark.linux_isolation
def test_linux_trusted_final_symlink_policy_allows_only_beneath_nonmagic_target(
    tmp_path: Path,
) -> None:
    if sys.platform != "linux":
        pytest.skip("trusted executable symlink policy requires Linux openat2")
    target = tmp_path / "target"
    target.write_bytes(b"executable")
    target.chmod(0o700)
    link = tmp_path / "tool"
    link.symlink_to("target")
    with pytest.raises(LinuxCapabilityError):
        LinuxPathCapability.acquire_absolute(link, expected="regular")
    with LinuxPathCapability.acquire_absolute(
        link,
        expected="regular",
        allow_symlinks=True,
        executable=True,
    ) as capability:
        assert capability.st_ino == target.stat().st_ino

    outside = tmp_path.parent / f"{tmp_path.name}-outside-tool"
    outside.write_bytes(b"outside")
    outside.chmod(0o700)
    escaping = tmp_path / "escaping"
    escaping.symlink_to(f"../{outside.name}")
    with pytest.raises(LinuxCapabilityError):
        LinuxPathCapability.acquire_absolute(
            escaping,
            expected="regular",
            allow_symlinks=True,
            executable=True,
        )
    with pytest.raises(LinuxCapabilityError):
        LinuxPathCapability.acquire_absolute(
            "/proc/self/fd/0",
            expected="regular",
            allow_symlinks=True,
        )


@pytest.mark.linux_isolation
def test_linux_shared_walker_repeats_counts_and_rejects_symlink(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "nested").mkdir()
    (root / "nested" / "a").write_bytes(b"a" * 10)
    (root / "b").write_bytes(b"b" * 20)
    with _linux_capability(root) as capability:
        first = measure_tree_usage(capability)
        second = measure_tree_usage(capability)
        assert first == second
        assert first.file_count == 2
        assert first.size_bytes == 30
        (root / "link").symlink_to(root / "b")
        with pytest.raises(ResourceCeilingError, match="symlink or special"):
            measure_tree_usage(capability)


@pytest.mark.linux_isolation
@pytest.mark.parametrize(
    ("profile", "source_name", "destination"),
    [
        ("agent", "workspace", "/workspace"),
        ("capture", "submitted", "/submitted/sentinel.txt"),
        ("capture", "repository", "/workspace"),
        ("evaluator", "workspace", "/workspace"),
        ("evaluator", "oracle", "/opt/measure-twice/oracle"),
        ("evaluator", "runtime", "/opt/measure-twice/runtime"),
    ],
)
def test_linux_every_builder_root_transfers_original_inode_after_symlink_swap(
    tmp_path: Path,
    profile: str,
    source_name: str,
    destination: str,
) -> None:
    submitted = tmp_path / "submitted"
    repository = tmp_path / "repository"
    workspace = tmp_path / "workspace"
    oracle = tmp_path / "oracle"
    runtime = tmp_path / "runtime"
    for directory in (submitted, repository, workspace, oracle, runtime):
        directory.mkdir()
        (directory / "sentinel.txt").write_text(f"original-{source_name}", encoding="utf-8")
    selected = {
        "submitted": submitted,
        "repository": repository,
        "workspace": workspace,
        "oracle": oracle,
        "runtime": runtime,
    }[source_name]
    moved = tmp_path / f"moved-{source_name}"
    outside = tmp_path / f"outside-{source_name}"
    outside.mkdir()
    (outside / "sentinel.txt").write_text("outside-canary", encoding="utf-8")
    race = _RaceBarrier()

    with _manual_preflight(tmp_path) as preflight:
        if profile == "agent":

            def operation() -> SandboxLaunch:
                return build_agent_sandbox(
                    preflight,
                    workspace=workspace,
                    command=("/usr/bin/true",),
                    _race_hook=race.hook("agent-workspace"),
                )
        elif profile == "capture":
            label = "capture-submitted" if source_name == "submitted" else "capture-repository"

            def operation() -> SandboxLaunch:
                return build_capture_sandbox(
                    preflight,
                    submitted_tree=submitted,
                    capture_repository=repository,
                    command=("/usr/bin/true",),
                    _race_hook=race.hook(label),
                )
        else:

            def operation() -> SandboxLaunch:
                return build_evaluator_sandbox(
                    preflight,
                    workspace=workspace,
                    oracle=oracle,
                    runtime=runtime,
                    command=("/usr/bin/true",),
                    ceilings=_ceilings(),
                    _race_hook=race.hook(f"evaluator-{source_name}"),
                )

        def mutate() -> None:
            selected.rename(moved)
            selected.symlink_to(outside, target_is_directory=True)

        value, error = race.run(operation, mutate)
        assert error is None
        launch = cast("SandboxLaunch", value)
        request = _request_from_launch(launch)
        try:
            by_fd = {capability.fd: capability for capability in request._inherited_capabilities}
            mounts = {}
            for index, argument in enumerate(request.argv):
                if argument not in {"--bind-fd", "--ro-bind-fd"}:
                    continue
                source_fd = request.argv[index + 1]
                mount_destination = request.argv[index + 2]
                if source_fd == EVALUATOR_WORKSPACE_FD_TOKEN:
                    assert profile == "evaluator"
                    assert mount_destination == "/workspace"
                    scratch = request._evaluator_scratch
                    assert scratch is not None
                    mounts[mount_destination] = scratch.source_capability()
                else:
                    mounts[mount_destination] = by_fd[int(source_fd)]
            mounted = mounts[destination]
            if stat.S_ISDIR(mounted.st_mode):
                sentinel_fd = os.open("sentinel.txt", os.O_RDONLY, dir_fd=mounted.fd)
                try:
                    observed = os.read(sentinel_fd, 128)
                finally:
                    os.close(sentinel_fd)
            else:
                observed = os.read(mounted.fd, 128)
            assert observed == f"original-{source_name}".encode()
            assert b"outside-canary" not in observed
        finally:
            request.close()
            launch.close()


@pytest.mark.linux_isolation
def test_linux_capture_direct_child_identity_change_fails_closed(tmp_path: Path) -> None:
    submitted = tmp_path / "submitted"
    repository = tmp_path / "repository"
    outside = tmp_path / "outside.txt"
    submitted.mkdir()
    repository.mkdir()
    child = submitted / "child.txt"
    child.write_text("original", encoding="utf-8")
    outside.write_text("outside-canary", encoding="utf-8")
    moved = submitted / "moved.txt"
    race = _RaceBarrier()
    with _manual_preflight(tmp_path) as preflight:

        def operation() -> SandboxLaunch:
            return build_capture_sandbox(
                preflight,
                submitted_tree=submitted,
                capture_repository=repository,
                command=("/usr/bin/true",),
                _race_hook=race.hook("capture-child:child.txt"),
            )

        def mutate() -> None:
            child.rename(moved)
            child.symlink_to(outside)

        value, error = race.run(operation, mutate)
        assert value is None
        # Post-acquisition rebinding is classified drift, not a generic capability error: the
        # capture path must still fail closed, and the specific class is what distinguishes
        # model-caused churn from harness failure downstream.
        assert isinstance(error, LinuxTreeDriftError)
        assert "changed after acquisition" in str(error)
        assert outside.read_text(encoding="utf-8") == "outside-canary"


@pytest.mark.linux_isolation
def test_linux_live_scanner_child_drift_records_inconclusive_sample_and_retries(
    tmp_path: Path,
) -> None:
    """Live churn is an inconclusive sample, never a harness failure the model can trigger."""

    if sys.platform != "linux":
        pytest.skip("live evaluator-tree scanner requires Linux")
    workspace = tmp_path / "live-workspace"
    workspace.mkdir()
    child = workspace / "child.txt"
    child.write_text("original", encoding="utf-8")
    moved = workspace / "moved.txt"
    race = _RaceBarrier()
    executable = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    request = ProcessRequest.create(
        argv=(executable, "-I", "-c", "import time; time.sleep(30)"),
        stdin="",
        cwd=tmp_path.resolve(),
        environment={},
        timeout_s=2,
        stream_limit_bytes=1024,
        resource_limits=ProcessResourceLimits(tree_files=100, tree_bytes=1024 * 1024),
        tree_root=workspace,
        _tree_before_open=race.hook("child.txt"),
    )

    # Rename only.  The entry identity changes underneath the held descriptor, so the live
    # walk observes real drift, but the tree it settles into is stable and policy-valid --
    # which is what lets the later authoritative scan succeed and keeps this test about the
    # live sample alone.
    def mutate() -> None:
        child.rename(moved)

    value, error = race.run(lambda: run_process(request), mutate)
    assert error is None
    assert value is not None
    assert value.termination == "timeout"
    # The drifted sample is retained as audit evidence and contributes no usage claim.
    assert value.tree_sample_inconclusive_count >= 1
    assert value.resource_limit is None
    # No outside-canary assertion here on purpose: this mutation creates no symlink, so
    # nothing outside the tree is ever reachable and such an assertion could not fail.
    # The symlink case is owned by the stable-policy-violation test below.


@pytest.mark.linux_isolation
def test_linux_terminal_scanner_child_identity_change_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real rename-to-symlink raced into the authoritative scan fails closed."""

    if sys.platform != "linux":
        pytest.skip("terminal evaluator-tree scanner requires Linux")
    workspace = tmp_path / "terminal-workspace"
    workspace.mkdir()
    child = workspace / "child.txt"
    child.write_text("original", encoding="utf-8")
    moved = workspace / "moved.txt"
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-canary", encoding="utf-8")

    # `next_tree_scan` starts at 0.0, so the monitor's first sample always walks the tree; a
    # very long interval then prevents any second live walk.  Arrival 1 is therefore the live
    # scan and arrival 2 is the terminal scan, which is what makes racing the terminal scan
    # deterministic rather than a coin flip between the two.
    monkeypatch.setattr(process_module, "_TREE_POLL_INTERVAL_S", 3600.0)
    race = _RaceBarrier()
    executable = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    request = ProcessRequest.create(
        argv=(executable, "-I", "-c", "import time; time.sleep(30)"),
        stdin="",
        cwd=tmp_path.resolve(),
        environment={},
        timeout_s=1,
        stream_limit_bytes=1024,
        resource_limits=ProcessResourceLimits(tree_files=100, tree_bytes=1024 * 1024),
        tree_root=workspace,
        _tree_before_open=race.hook("child.txt", ordinal=2),
    )

    def mutate() -> None:
        child.rename(moved)
        child.symlink_to(outside)

    value, error = race.run(
        lambda: run_process(request),
        mutate,
        enter_timeout_s=20.0,
        join_timeout_s=20.0,
    )
    assert value is None
    assert isinstance(error, ProcessExecutionError)
    assert "changed during post-cleanup validation" in str(error)
    # A real symlink to `outside` exists by now, so this canary can actually fail if the
    # authoritative scan ever followed the replacement instead of the held descriptor.
    assert outside.read_text(encoding="utf-8") == "outside-canary"


@pytest.mark.linux_isolation
def test_linux_terminal_scanner_stable_policy_violation_stays_a_model_tree_outcome(
    tmp_path: Path,
) -> None:
    """A stable model-created symlink is a typed model-tree outcome, not harness failure."""

    if sys.platform != "linux":
        pytest.skip("terminal evaluator-tree scanner requires Linux")
    workspace = tmp_path / "policy-workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-canary", encoding="utf-8")
    # Stable for the whole run rather than raced: the live sample and the authoritative scan
    # both observe the identical policy-violating tree, which is exactly the case Step 27 must
    # be able to score as a model `forbidden-edit` instead of an evaluator-infrastructure fault.
    (workspace / "escape.txt").symlink_to(outside)
    executable = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    request = ProcessRequest.create(
        argv=(executable, "-I", "-c", "raise SystemExit(0)"),
        stdin="",
        cwd=tmp_path.resolve(),
        environment={},
        timeout_s=10,
        stream_limit_bytes=1024,
        resource_limits=ProcessResourceLimits(tree_files=100, tree_bytes=1024 * 1024),
        tree_root=workspace,
    )
    with pytest.raises(ModelTreeViolationError, match="violates submitted-output policy") as raised:
        run_process(request)
    # The whole point of the typed distinction: this must NOT arrive as infrastructure failure.
    assert not isinstance(raised.value, ProcessExecutionError)
    assert outside.read_text(encoding="utf-8") == "outside-canary"


@pytest.mark.linux_isolation
def test_linux_live_scanner_capability_fault_is_evaluator_infrastructure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raw capability/I/O fault at the live seam is infrastructure, never a model outcome."""

    if sys.platform != "linux":
        pytest.skip("live evaluator-tree scanner requires Linux")
    workspace = tmp_path / "capability-workspace"
    workspace.mkdir()
    # Exactly one live walk: `next_tree_scan` starts at 0.0 so the monitor's first sample always
    # runs, and a long interval suppresses cleanup's pre-kill pass.  The surfaced cause chain
    # therefore belongs to one identified scan rather than whichever poll happened to arrive.
    monkeypatch.setattr(process_module, "_TREE_POLL_INTERVAL_S", 3600.0)
    executable = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    request = ProcessRequest.create(
        argv=(executable, "-I", "-c", "import time; time.sleep(30)"),
        stdin="",
        cwd=tmp_path.resolve(),
        environment={},
        timeout_s=5,
        stream_limit_bytes=1024,
        resource_limits=ProcessResourceLimits(tree_files=100, tree_bytes=1024 * 1024),
        tree_root=workspace,
    )
    # Kill the pinned capability itself.  Destroying the directory underneath the held FD does
    # NOT work here: this kernel lets openat2(dirfd, ".", RESOLVE_BENEATH) succeed on an unlinked
    # directory, so walk_tree just reports an empty tree (measured -- see
    # .review-deep/step26-coverage/B5-spec.md).  Closing is the one mechanism that produces a
    # *plain* LinuxCapabilityError from production code rather than a fabricated one: no
    # model-authored tree content can ever reach this branch, because every content-shaped
    # mutation is typed drift/policy/limit first.  `run_process` owns and re-closes this
    # capability on cleanup, and close() is idempotent.
    assert request._tree_capability is not None
    request._tree_capability.close()

    with pytest.raises(ProcessExecutionError) as raised:
        run_process(request)

    assert "evaluator tree inspection failed closed" in str(raised.value)
    assert not isinstance(raised.value, ModelTreeViolationError)
    inner = raised.value.__cause__
    assert isinstance(inner, ProcessExecutionError)
    assert str(inner) == "evaluator tree inspection failed closed"
    cause = inner.__cause__
    # The clause itself: a *plain* capability fault, not one of the three typed tree outcomes.
    assert isinstance(cause, LinuxCapabilityError)
    assert not isinstance(cause, (LinuxTreeDriftError, LinuxTreePolicyError, LinuxTreeLimitError))


@pytest.mark.linux_isolation
def test_linux_terminal_scanner_capability_fault_is_evaluator_infrastructure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The authoritative scan reports a raw capability fault as infrastructure, not a model zero."""

    if sys.platform != "linux":
        pytest.skip("terminal evaluator-tree scanner requires Linux")
    workspace = tmp_path / "terminal-capability-workspace"
    workspace.mkdir()
    child = workspace / "child.txt"
    child.write_text("original", encoding="utf-8")
    # Arrival 1 is the monitor's first and -- with this interval -- only live sample.  Blocking
    # there lets the capability be closed while that walk is already streaming its children off
    # descriptors it opened before the seam, so the live scan still completes normally and only
    # the post-quiescence authoritative scan meets the dead capability.  That ordering is what
    # makes this a test of seam 1461 specifically rather than of seam 1417.
    monkeypatch.setattr(process_module, "_TREE_POLL_INTERVAL_S", 3600.0)
    race = _RaceBarrier()
    executable = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    request = ProcessRequest.create(
        argv=(executable, "-I", "-c", "import time; time.sleep(30)"),
        stdin="",
        cwd=tmp_path.resolve(),
        environment={},
        timeout_s=1,
        stream_limit_bytes=1024,
        resource_limits=ProcessResourceLimits(tree_files=100, tree_bytes=1024 * 1024),
        tree_root=workspace,
        _tree_before_open=race.hook("child.txt"),
    )

    # The in-flight live walk keeps working off already-open descriptors; the next walk_tree
    # call -- validate_terminal_tree's -- is the one that asks the capability to reopen.
    def mutate() -> None:
        assert request._tree_capability is not None
        request._tree_capability.close()

    value, error = race.run(
        lambda: run_process(request),
        mutate,
        enter_timeout_s=20.0,
        join_timeout_s=20.0,
    )
    assert value is None
    assert isinstance(error, ProcessExecutionError)
    assert "evaluator terminal tree inspection failed closed" in str(error)
    # Distinct from its neighbour branch: a vanished capability is never a Step-27 model zero.
    assert not isinstance(error, ModelTreeViolationError)
    assert "violates submitted-output policy" not in str(error)
    cause = error.__cause__
    assert isinstance(cause, LinuxCapabilityError)
    assert not isinstance(cause, (LinuxTreeDriftError, LinuxTreePolicyError, LinuxTreeLimitError))


@pytest.mark.linux_isolation
@pytest.mark.parametrize("failure_stage", ["setup", "execute", "verify"])
def test_linux_bubblewrap_probe_partial_setup_failure_leaks_no_fd_or_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    with _linux_capability(tmp_path) as root:
        baseline = len(os.listdir("/proc/self/fd"))

        def pinned_subprocess(
            _executable: object,
            argv: object,
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[bytes]:
            is_version = "--version" in cast("tuple[str, ...]", tuple(cast("Any", argv)))
            return subprocess.CompletedProcess(
                [],
                0 if is_version or failure_stage == "verify" else 1,
                b"bubblewrap test\n" if is_version else b"",
                b"",
            )

        monkeypatch.setattr(isolation_module, "_pinned_subprocess", pinned_subprocess)

        def fail_write(*_args: object, **_kwargs: object) -> None:
            raise OSError("injected write failure")

        if failure_stage == "setup":
            monkeypatch.setattr(isolation_module, "_write_all", fail_write)
            expected: type[BaseException] = OSError
            match = "injected write failure"
        else:
            expected = IsolationUnavailableError
            match = "compatible Bubblewrap"
        with pytest.raises(expected, match=match):
            isolation_module._probe_bubblewrap(root, (), root)
        assert not tuple(tmp_path.glob(".measure-twice-bwrap-probe-*"))
        assert len(os.listdir("/proc/self/fd")) == baseline


@pytest.mark.linux_isolation
def test_linux_capture_omits_submitted_git_and_has_only_trusted_environment(
    tmp_path: Path,
) -> None:
    submitted = tmp_path / "submitted"
    repository = tmp_path / "capture-repository"
    submitted.mkdir()
    repository.mkdir()
    (submitted / "visible.txt").write_text("submitted", encoding="utf-8")
    (submitted / ".git").mkdir()
    (submitted / ".git" / "config").write_text("credential-sentinel", encoding="utf-8")
    code = (
        "import json,os,pathlib; "
        "print(json.dumps({"
        "'git_absent':not pathlib.Path('/submitted/.git').exists(),"
        "'visible':pathlib.Path('/submitted/visible.txt').read_text(),"
        "'environment':dict(os.environ)},sort_keys=True))"
    )
    with _real_preflight(tmp_path) as preflight:
        with build_capture_sandbox(
            preflight,
            submitted_tree=submitted,
            capture_repository=repository,
            command=("/usr/bin/python3", "-I", "-c", code),
        ) as launch:
            request = _request_from_launch(launch)
            result = run_process(request)
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {
        "environment": dict(CAPTURE_GIT_ENVIRONMENT),
        "git_absent": True,
        "visible": "submitted",
    }


@pytest.mark.linux_isolation
def test_linux_agent_namespace_persists_only_workspace_writes(tmp_path: Path) -> None:
    workspace = tmp_path / "agent-workspace"
    workspace.mkdir()
    outside = tmp_path / "outside-sentinel.txt"
    code = (
        "import json,os,pathlib; "
        "pathlib.Path('/workspace/persisted.txt').write_text('ok'); "
        f"outside=pathlib.Path({str(outside)!r}); "
        "escaped=True; "
        "\ntry: outside.write_text('escaped')\nexcept OSError: escaped=False\n"
        "print(json.dumps({"
        "'escaped':escaped,'home':os.environ.get('HOME'),"
        "'oracle':pathlib.Path('/opt/measure-twice/oracle').exists(),"
        "'procfs_matches_pid':pathlib.Path('/proc/self').samefile(f'/proc/{os.getpid()}'),"
        "'suite':pathlib.Path('/suite').exists(),"
        "'run':pathlib.Path('/run-store').exists()}))"
    )
    with _real_preflight(tmp_path) as preflight:
        with build_agent_sandbox(
            preflight,
            workspace=workspace,
            command=("/usr/bin/python3", "-I", "-c", code),
        ) as launch:
            result = _run_launch(launch)
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {
        "escaped": False,
        "home": SANDBOX_HOME,
        "oracle": False,
        "procfs_matches_pid": True,
        "suite": False,
        "run": False,
    }
    assert (workspace / "persisted.txt").read_text(encoding="utf-8") == "ok"
    assert not outside.exists()


@pytest.mark.linux_isolation
def test_linux_evaluator_hostile_canaries_all_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "evaluator-workspace"
    oracle = tmp_path / "oracle"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    oracle.mkdir()
    _copy_fixture(runtime, "hostile_probe.py")
    (oracle / "oracle-sentinel.txt").write_text("oracle-original", encoding="utf-8")
    (runtime / "runtime-sentinel.txt").write_text("runtime-original", encoding="utf-8")
    host_sentinel = tmp_path / "host-sentinel.txt"
    credential_sentinel = tmp_path / "credential-sentinel.txt"
    outside = tmp_path / "outside-write.txt"
    host_sentinel.write_text("host-only", encoding="utf-8")
    credential_sentinel.write_text("credential-only", encoding="utf-8")
    credential_value = "credential-environment-canary"
    monkeypatch.setenv("CREDENTIAL_SENTINEL", credential_value)
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tcp.bind(("127.0.0.1", 0))
    tcp.listen()
    udp.bind(("127.0.0.1", 0))
    udp_echo_stop = threading.Event()

    def echo_udp_canary() -> None:
        udp.settimeout(0.05)
        while not udp_echo_stop.is_set():
            try:
                message, peer = udp.recvfrom(64)
            except TimeoutError:
                continue
            except OSError:
                return
            if message == b"udp-canary":
                try:
                    udp.sendto(b"udp-canary", peer)
                except OSError:
                    return
                return

    udp_echo = threading.Thread(target=echo_udp_canary, daemon=True)
    payload = {
        "credential_path": str(credential_sentinel),
        "credential_value": credential_value,
        "host_path": str(host_sentinel),
        "outside_path": str(outside),
        "tcp_port": tcp.getsockname()[1],
        "udp_port": udp.getsockname()[1],
    }
    try:
        with _real_preflight(tmp_path) as preflight:
            with build_evaluator_sandbox(
                preflight,
                workspace=workspace,
                oracle=oracle,
                runtime=runtime,
                command=(
                    "/usr/bin/python3",
                    "-I",
                    "/opt/measure-twice/runtime/hostile_probe.py",
                ),
                ceilings=load_execution_profile(EXECUTION_PROFILE).ceilings,
            ) as launch:
                udp_echo.start()
                result = _run_launch(launch, stdin=json.dumps(payload))
    finally:
        udp_echo_stop.set()
        tcp.close()
        if udp_echo.ident is not None:
            udp_echo.join(timeout=1)
            assert not udp_echo.is_alive()
        udp.close()
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {
        "credential_read": False,
        "dns_reached": False,
        "host_read": False,
        "oracle_before": "oracle-original",
        "oracle_mutated": False,
        "outside_write": False,
        "parent_credential_environment": False,
        "process_environment_credential": False,
        "run_store_visible": False,
        "runtime_before": "runtime-original",
        "runtime_mutated": False,
        "suite_visible": False,
        "tcp_reached": False,
        "udp_reached": False,
        "workspace_write": True,
    }
    assert (oracle / "oracle-sentinel.txt").read_text(encoding="utf-8") == "oracle-original"
    assert (runtime / "runtime-sentinel.txt").read_text(encoding="utf-8") == "runtime-original"
    assert not outside.exists()


@pytest.mark.linux_isolation
def test_linux_timeout_leaves_no_detached_child(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    oracle = tmp_path / "oracle"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    oracle.mkdir()
    _copy_fixture(runtime, "detached_child.py")
    with _real_preflight(tmp_path) as preflight:
        with build_evaluator_sandbox(
            preflight,
            workspace=workspace,
            oracle=oracle,
            runtime=runtime,
            command=(
                "/usr/bin/python3",
                "-I",
                "/opt/measure-twice/runtime/detached_child.py",
                "/workspace/escaped.txt",
                "/workspace/ready.txt",
                "/workspace/release.txt",
            ),
            ceilings=load_execution_profile(EXECUTION_PROFILE).ceilings,
        ) as launch:
            result = _run_launch(launch, timeout=5)
            assert result.termination == "timeout"
            # The evaluator workspace is a private tmpfs, so the sandbox's own markers are
            # only reachable through the root descriptor the harness retains.  Asserting the
            # host directory stays empty keeps that isolation explicit: without it, a marker
            # that simply became invisible would read exactly like a contained child.
            assert not (workspace / "ready.txt").exists()
            assert not (workspace / "escaped.txt").exists()
            retained = Path(f"/proc/self/fd/{launch.terminal_tree_capability().fd}")
            assert (retained / "ready.txt").is_file()
            # Release inside the same tmpfs a surviving detached child would still be polling.
            (retained / "release.txt").write_text("release", encoding="utf-8")
            _assert_path_stays_absent(retained / "escaped.txt")


@pytest.mark.linux_isolation
def test_linux_evaluator_kills_a_descendant_that_outlives_the_nominal_target(
    tmp_path: Path,
) -> None:
    """A cleanly-exiting target may not leave a reparented descendant alive in its scope."""

    workspace = tmp_path / "workspace-outliving"
    oracle = tmp_path / "oracle-outliving"
    runtime = tmp_path / "runtime-outliving"
    workspace.mkdir()
    oracle.mkdir()
    _copy_fixture(runtime, "outliving_child.py")
    with _real_preflight(tmp_path) as preflight:
        with build_evaluator_sandbox(
            preflight,
            workspace=workspace,
            oracle=oracle,
            runtime=runtime,
            command=(
                "/usr/bin/python3",
                "-I",
                "/opt/measure-twice/runtime/outliving_child.py",
                "/workspace/escaped.txt",
                "/workspace/ready.txt",
                "/workspace/release.txt",
            ),
            # Generous CPU headroom on purpose: the descendant polls inside the same cgroup,
            # and this canary is about containment, not about racing a sampled CPU ceiling.
            ceilings=_ceilings(files=16, cpu=30),
        ) as launch:
            result = _run_launch(launch, timeout=20)
            # The nominal target exits cleanly; containment may not depend on a timeout kill.
            assert result.termination == "exited", result.stderr
            assert result.exit_code == 0
            assert result.resource_limit is None
            retained = Path(f"/proc/self/fd/{launch.terminal_tree_capability().fd}")
            # The descendant was genuinely alive when its parent exited.
            assert (retained / "ready.txt").is_file()
            (retained / "release.txt").write_text("release", encoding="utf-8")
            _assert_path_stays_absent(retained / "escaped.txt")


@pytest.mark.linux_isolation
def test_linux_evaluator_inherits_rlimit_nofile_through_sandbox_launch(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-nofile"
    oracle = tmp_path / "oracle-nofile"
    runtime = tmp_path / "runtime-nofile"
    workspace.mkdir()
    oracle.mkdir()
    runtime.mkdir()
    code = """
import errno
import json
import os
import resource

limits = resource.getrlimit(resource.RLIMIT_NOFILE)
descriptors = []
try:
    while True:
        descriptors.append(os.open('/dev/null', os.O_RDONLY))
except OSError as exc:
    print(
        json.dumps(
            {'errno': exc.errno, 'hard': limits[1], 'opened': len(descriptors), 'soft': limits[0]}
        )
    )
finally:
    for descriptor in descriptors:
        os.close(descriptor)
"""
    with _real_preflight(tmp_path) as preflight:
        with build_evaluator_sandbox(
            preflight,
            workspace=workspace,
            oracle=oracle,
            runtime=runtime,
            command=("/usr/bin/python3", "-I", "-c", code),
            ceilings=_ceilings(files=64),
        ) as launch:
            result = _run_launch(launch)
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["errno"] == errno.EMFILE
    assert payload["soft"] == 64
    assert payload["hard"] == 64
    assert 0 < payload["opened"] < 64


@pytest.mark.linux_isolation
def test_linux_evaluator_bubblewrap_uses_private_tmpfs_fd_and_retains_terminal_fd(
    tmp_path: Path,
) -> None:
    """The real Bubblewrap path writes through an outer-private tmpfs FD retained by the parent."""

    workspace = tmp_path / "workspace-detached-tmpfs"
    oracle = tmp_path / "oracle-detached-tmpfs"
    runtime = tmp_path / "runtime-detached-tmpfs"
    workspace.mkdir()
    oracle.mkdir()
    runtime.mkdir()
    host_tmp_identity = _host_private_mountpoint_identity()
    with _real_preflight(tmp_path) as preflight:
        with build_evaluator_sandbox(
            preflight,
            workspace=workspace,
            oracle=oracle,
            runtime=runtime,
            command=(
                "/usr/bin/python3",
                "-I",
                "-c",
                "from pathlib import Path; "
                "Path('/workspace/terminal.txt').write_bytes(b'terminal')",
            ),
            ceilings=_ceilings(files=8),
        ) as launch:
            result = _run_launch(launch)
            assert result.exit_code == 0, result.stderr
            terminal_root = launch.terminal_tree_capability()
            with _host_private_mountpoint_capability() as host_tmp_fd:
                assert terminal_root.filesystem_magic != host_tmp_fd.filesystem_magic
            with terminal_root.open_beneath(
                "terminal.txt",
                expected="regular",
                display_path="/workspace/terminal.txt",
            ) as terminal_file:
                assert os.read(terminal_file.fd, 8) == b"terminal"
            assert walk_tree(terminal_root).file_count == 1
    assert _host_private_mountpoint_identity() == host_tmp_identity


@pytest.mark.linux_isolation
def test_linux_evaluator_release_barrier_holds_target_until_real_control_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No evaluator target byte crosses the barrier before the real cgroup readback completes."""

    workspace = tmp_path / "workspace-barrier"
    oracle = tmp_path / "oracle-barrier"
    runtime_directory = tmp_path / "runtime-barrier"
    for directory in (workspace, oracle, runtime_directory):
        directory.mkdir()
    ceilings = _ceilings(memory=256 * 1024 * 1024, processes=16, files=8)
    real_read = process_module._read_directory_text
    reads: list[str] = []

    def recording_read(capability: LinuxPathCapability, name: str) -> str:
        value = real_read(capability, name)
        reads.append(name)
        return value

    real_handshake = process_module._receive_evaluator_handshake
    observed: dict[str, Any] = {}

    def observing_handshake(
        control: socket.socket,
        request: ProcessRequest,
        runtime: Any,
        scope_relative_path: str,
    ) -> LinuxPathCapability:
        # The real handshake: real adopt, real validate_before_release, real _verify_readback.
        scratch_capability = real_handshake(control, request, runtime, scope_relative_path)
        state = runtime.resource_guard
        observed["reads"] = tuple(reads)
        observed["validated"] = state.validated_for_release
        observed["owner"] = state.outer_owner_identity
        observed["magic"] = state.capability.filesystem_magic
        observed["controls"] = {
            name: real_read(state.capability, name).strip()
            for name in ("memory.max", "memory.swap.max", "pids.max", "cpu.max")
        }
        # Still behind the barrier: _start_process has not sent b"R", so the supervisor is parked
        # in control.recv(1), has forked nothing, and no target byte can reach the stdout pipe.
        owner_pid = state.outer_owner_identity[0]
        deadline = time.monotonic() + _BARRIER_DWELL_S
        while time.monotonic() < deadline:
            assert not select.select([runtime.proc.stdout], [], [], 0)[0], (
                "evaluator target wrote a byte before the release barrier"
            )
            assert not process_module._posix_direct_children(owner_pid), (
                "evaluator supervisor forked the target before the release barrier"
            )
            time.sleep(0.02)
        observed["dwelled"] = True
        return scratch_capability

    monkeypatch.setattr(process_module, "_read_directory_text", recording_read)
    monkeypatch.setattr(process_module, "_receive_evaluator_handshake", observing_handshake)
    with _real_preflight(tmp_path) as preflight:
        with build_evaluator_sandbox(
            preflight,
            workspace=workspace,
            oracle=oracle,
            runtime=runtime_directory,
            command=("/usr/bin/python3", "-I", "-c", "import os; os.write(1, b'FIRST')"),
            ceilings=ceilings,
        ) as launch:
            result = _run_launch(launch, timeout=20)
    assert observed["dwelled"] is True
    assert observed["validated"] is True
    assert observed["owner"] is not None
    assert observed["magic"] == process_module._CGROUP2_SUPER_MAGIC
    controls = observed["controls"]
    assert controls["memory.max"] == str(ceilings.evaluator_memory_bytes)
    assert controls["memory.swap.max"] == "0"
    assert controls["pids.max"] == str(ceilings.evaluator_processes)
    quota, period = (int(piece) for piece in controls["cpu.max"].split())
    assert quota > 0 and period > 0
    assert quota * 100 == ceilings.evaluator_cpu_bandwidth_percent * period
    assert {
        "memory.max",
        "memory.swap.max",
        "pids.max",
        "cpu.max",
        "cgroup.procs",
    }.issubset(set(observed["reads"]))
    # Not vacuous: the target really did execute, after the barrier.
    assert result.exit_code == 0, result.stderr
    assert result.stdout == b"FIRST"


@pytest.mark.linux_isolation
@pytest.mark.parametrize(
    ("control", "replacement", "message"),
    [
        ("memory.max", "1\n", "memory.max readback mismatch"),
        ("memory.swap.max", "max\n", "memory.swap.max readback mismatch"),
        ("pids.max", "9999\n", "pids.max readback mismatch"),
        ("cpu.max", "50000 200000\n", "cpu.max readback mismatch"),
    ],
)
def test_linux_evaluator_control_readback_mismatch_fails_closed_before_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control: str,
    replacement: str,
    message: str,
) -> None:
    """A single mismatched effective control fails closed as evaluator infrastructure."""

    workspace = tmp_path / "workspace-mismatch"
    oracle = tmp_path / "oracle-mismatch"
    runtime_directory = tmp_path / "runtime-mismatch"
    for directory in (workspace, oracle, runtime_directory):
        directory.mkdir()
    host_tmp_identity = _host_private_mountpoint_identity()
    real_read = process_module._read_directory_text

    def corrupt_one_control(capability: LinuxPathCapability, name: str) -> str:
        value = real_read(capability, name)
        return replacement if name == control else value

    monkeypatch.setattr(process_module, "_read_directory_text", corrupt_one_control)
    with _real_preflight(tmp_path) as preflight:
        with build_evaluator_sandbox(
            preflight,
            workspace=workspace,
            oracle=oracle,
            runtime=runtime_directory,
            command=("/usr/bin/python3", "-I", "-c", "import os; os.write(1, b'FIRST')"),
            ceilings=_ceilings(memory=256 * 1024 * 1024, processes=16, files=8),
        ) as launch:
            with pytest.raises(ProcessExecutionError, match=message):
                _run_launch(launch, timeout=20)
    assert _host_private_mountpoint_identity() == host_tmp_identity


@pytest.mark.linux_isolation
def test_linux_evaluator_failed_materialization_leaves_host_private_mountpoint_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-release failure tears down the private mount namespace without host mutation."""

    workspace = tmp_path / "workspace-failed-detach"
    oracle = tmp_path / "oracle-failed-detach"
    runtime = tmp_path / "runtime-failed-detach"
    workspace.mkdir()
    oracle.mkdir()
    runtime.mkdir()
    host_tmp_identity = _host_private_mountpoint_identity()

    def fail_materialization(*_args: object, **_kwargs: object) -> None:
        raise LinuxCapabilityError("injected private tmpfs materialization failure")

    monkeypatch.setattr(process_module, "copy_tree", fail_materialization)
    with _real_preflight(tmp_path) as preflight:
        with build_evaluator_sandbox(
            preflight,
            workspace=workspace,
            oracle=oracle,
            runtime=runtime,
            command=("/usr/bin/python3", "-I", "-c", "raise SystemExit(0)"),
            ceilings=_ceilings(files=8),
        ) as launch:
            with pytest.raises(ProcessExecutionError, match="materialize evaluator applied tree"):
                _run_launch(launch)
    assert _host_private_mountpoint_identity() == host_tmp_identity


@pytest.mark.linux_isolation
@pytest.mark.parametrize(
    ("operation", "provenance"),
    [
        # CPU is cumulative cgroup accounting read at monitor ticks: a sampled scoring
        # threshold that may overshoot.  Memory and task ceilings are kernel-enforced cgroup
        # guards.  The two-layer contract is observable ONLY through provenance -- a silent
        # fall back to /proc polling reports the identical resource name.
        ("cpu", "sampled-threshold"),
        ("memory", "hard-guard"),
        ("processes", "hard-guard"),
    ],
)
def test_linux_evaluator_aggregate_cpu_memory_and_process_limits(
    tmp_path: Path,
    operation: str,
    provenance: str,
) -> None:
    workspace = tmp_path / f"workspace-{operation}"
    oracle = tmp_path / f"oracle-{operation}"
    runtime = tmp_path / f"runtime-{operation}"
    workspace.mkdir()
    oracle.mkdir()
    _copy_fixture(runtime, "resource_probe.py")
    cpu_seconds = 1 if operation == "cpu" else 10
    memory_bytes = 128 * 1024 * 1024
    processes = 8
    with _real_preflight(tmp_path) as preflight:
        with build_evaluator_sandbox(
            preflight,
            workspace=workspace,
            oracle=oracle,
            runtime=runtime,
            command=(
                "/usr/bin/python3",
                "-I",
                "/opt/measure-twice/runtime/resource_probe.py",
                operation,
            ),
            ceilings=_ceilings(
                cpu=cpu_seconds,
                memory=memory_bytes,
                processes=processes,
            ),
        ) as launch:
            # Generous wall clock on purpose.  This canary asserts that a RESOURCE ceiling
            # is what stops the run, so the wall clock must never be the thing that fires
            # first -- the ceiling depends on a kernel event (OOM kill, pids denial, walker
            # crossing) whose latency the test does not control.  A broken guard still
            # fails here, it just fails as 'timeout' after longer.
            result = _run_launch(launch, timeout=30)
    assert result.termination == "resource-limit"
    assert result.resource_limit == operation
    assert result.resource_limit_provenance == provenance
    configured = {"cpu": cpu_seconds, "memory": memory_bytes, "processes": processes}[operation]
    # The recorded limit is the control the kernel actually read back before release:
    # _verify_readback refuses to release the target unless memory.max/pids.max equal these.
    assert result.resource_limit_value == configured
    observed = result.resource_limit_observed
    assert observed is not None
    if operation == "cpu":
        # Sampled: cumulative usec//1e6 crossed the configured second and may overshoot.
        assert observed >= configured
    elif operation == "processes":
        # pids.peak cannot exceed the enforced pids.max, and a denied fork proves it reached it.
        assert observed == configured
    else:
        # memory.peak is bounded by the enforced memory.max and sits near it after the OOM kill.
        assert configured // 2 <= observed <= configured


@pytest.mark.linux_isolation
@pytest.mark.parametrize(
    ("children", "expected_limit"),
    [(16, "memory"), (4, None)],
    ids=["aggregate-crosses", "aggregate-stays-under"],
)
def test_linux_evaluator_multi_descendant_allocation_hits_the_hard_memory_guard(
    tmp_path: Path,
    children: int,
    expected_limit: str | None,
) -> None:
    """Descendants that each stay far under the bound must still be charged together.

    Only aggregate cgroup accounting can see this: every descendant holds one eighth of
    ``memory.max``, so the per-process ``RLIMIT``/``/proc``-RSS approach this step replaces stays
    silent.  The under-budget parametrization is the calibration anchor -- same fixture, same
    ceilings, fewer descendants -- and the guard must NOT fire.
    """

    chunk = 24 * 1024 * 1024
    memory_bytes = 192 * 1024 * 1024
    workspace = tmp_path / f"workspace-fan-out-{children}"
    oracle = tmp_path / f"oracle-fan-out-{children}"
    runtime = tmp_path / f"runtime-fan-out-{children}"
    workspace.mkdir()
    oracle.mkdir()
    _copy_fixture(runtime, "resource_probe.py")
    with _real_preflight(tmp_path) as preflight:
        with build_evaluator_sandbox(
            preflight,
            workspace=workspace,
            oracle=oracle,
            runtime=runtime,
            command=(
                "/usr/bin/python3",
                "-I",
                "/opt/measure-twice/runtime/resource_probe.py",
                "memory-fan-out",
                str(children),
                str(chunk),
            ),
            ceilings=_ceilings(cpu=10, memory=memory_bytes, processes=48),
        ) as launch:
            result = _run_launch(launch, timeout=20)
    if expected_limit is None:
        # Calibration anchor: the same fan-out below the aggregate bound must score clean.
        assert result.termination == "exited", result.stderr
        assert result.exit_code == 0, result.stderr
        assert result.resource_limit is None
        assert result.resource_limit_observed is None
        assert result.resource_limit_provenance is None
        assert result.stdout.decode("utf-8").strip().endswith(f"fan-out-complete:{children}")
        return
    assert result.termination == "resource-limit", result.stderr
    assert result.exit_code is None
    assert result.resource_limit == expected_limit
    # The recorded limit is the cgroup's configured aggregate bound, never an RLIMIT backstop.
    assert result.resource_limit_value == memory_bytes
    assert result.resource_limit_provenance == "hard-guard"
    observed = result.resource_limit_observed
    assert observed is not None
    # `observed` is cgroup `memory.peak`.  No descendant ever held more than `chunk`, so a value
    # clearing four whole descendants proves the charge was summed across the tree rather than
    # read off any single process.
    assert observed >= 4 * chunk


@pytest.mark.linux_isolation
@pytest.mark.parametrize(
    ("count", "size", "files", "file_bytes", "live_limit", "expected_observed", "message"),
    [
        # walk_tree raises on the FIRST crossing, so both observations are exact, not racy:
        # file-count crosses at files + 1; file-bytes crosses at the first cumulative sum
        # strictly greater than file_bytes (80 -> 160 for two 80-byte files).
        (3, 10, 2, 100, "file-count", 3, "file ceiling"),
        (2, 80, 10, 128, "file-bytes", 160, "byte ceiling"),
    ],
)
def test_linux_evaluator_terminal_file_ceilings(
    tmp_path: Path,
    count: int,
    size: int,
    files: int,
    file_bytes: int,
    live_limit: str,
    expected_observed: int,
    message: str,
) -> None:
    workspace = tmp_path / "workspace-files"
    oracle = tmp_path / "oracle-files"
    runtime = tmp_path / "runtime-files"
    workspace.mkdir()
    oracle.mkdir()
    _copy_fixture(runtime, "resource_probe.py")
    with _real_preflight(tmp_path) as preflight:
        with build_evaluator_sandbox(
            preflight,
            workspace=workspace,
            oracle=oracle,
            runtime=runtime,
            command=(
                "/usr/bin/python3",
                "-I",
                "/opt/measure-twice/runtime/resource_probe.py",
                "files",
                str(count),
                str(size),
            ),
            ceilings=_ceilings(files=files, file_bytes=file_bytes),
        ) as launch:
            # Generous wall clock on purpose.  This canary asserts that a RESOURCE ceiling
            # is what stops the run, so the wall clock must never be the thing that fires
            # first -- the ceiling depends on a kernel event (OOM kill, pids denial, walker
            # crossing) whose latency the test does not control.  A broken guard still
            # fails here, it just fails as 'timeout' after longer.
            result = _run_launch(launch, timeout=30)
            assert result.termination == "resource-limit"
            assert result.exit_code is None
            assert result.resource_limit == live_limit
            # A logical walker threshold is a sampled scoring rule.  It must never be
            # promoted to hard-guard: the tmpfs here is generously bounded and
            # _tmpfs_hard_exhaustion returns None, so physical exhaustion did NOT occur.
            assert result.resource_limit_provenance == "sampled-threshold"
            assert result.resource_limit_value == (
                files if live_limit == "file-count" else file_bytes
            )
            assert result.resource_limit_observed == expected_observed
            with pytest.raises(ResourceCeilingError, match=message):
                enforce_evaluator_tree_ceiling(launch)


@pytest.mark.linux_isolation
def test_linux_evaluator_parallel_large_writers_hit_the_physical_tmpfs_byte_envelope(
    tmp_path: Path,
) -> None:
    """Concurrent writers exhaust the private tmpfs itself, not the backing ext4 volume.

    The physical envelope is the only thing that can stop four unbounded writers, so a full
    ``f_bavail <= 0`` readback through the retained parent FD is what authorizes ``hard-guard``
    provenance carrying the tmpfs capacity instead of the configured logical byte ceiling.
    """

    if sys.platform != "linux":
        pytest.skip("private tmpfs envelope canaries require Linux")
    workspace = tmp_path / "workspace-parallel-writers"
    oracle = tmp_path / "oracle-parallel-writers"
    runtime = tmp_path / "runtime-parallel-writers"
    workspace.mkdir()
    oracle.mkdir()
    _copy_fixture(runtime, "resource_probe.py")
    writers = 4
    page_size = int(os.sysconf("SC_PAGE_SIZE"))
    byte_limit = 4 * 1024 * 1024
    ceilings = _ceilings(
        cpu=10,
        files=writers,
        file_bytes=byte_limit,
        tmpfs_bytes=byte_limit + writers * page_size,
    )
    host_tmp_identity = _host_private_mountpoint_identity()
    with _real_preflight(tmp_path) as preflight:
        with build_evaluator_sandbox(
            preflight,
            workspace=workspace,
            oracle=oracle,
            runtime=runtime,
            command=(
                "/usr/bin/python3",
                "-I",
                "/opt/measure-twice/runtime/resource_probe.py",
                "parallel-writers",
                str(writers),
                str(256 * 1024),
            ),
            ceilings=ceilings,
        ) as launch:
            result = _run_launch(launch, timeout=20)
            terminal_root = launch.terminal_tree_capability()
            values = os.fstatvfs(terminal_root.fd)
            capacity = int(values.f_blocks) * int(values.f_frsize)
            # The retained FD -- not a supervisor claim -- is what proves exhaustion.
            # `<= 0`: tmpfs reports a NEGATIVE remainder when block accounting overshoots
            # on a full filesystem (measured: -1).  Equality here would make this canary
            # flake red on a genuinely exhausted tmpfs -- and it is how the same equality
            # bug in the production predicate stayed hidden.
            assert values.f_bavail <= 0
            assert capacity == ceilings.evaluator_tmpfs_bytes
            assert result.termination == "resource-limit"
            assert result.exit_code is None
            assert result.resource_limit == "file-bytes"
            assert result.resource_limit_provenance == "hard-guard"
            # The recorded ceiling is the PHYSICAL tmpfs bound, never the logical byte ceiling.
            assert result.resource_limit_value == capacity
            assert result.resource_limit_value > ceilings.evaluator_file_bytes
            assert result.resource_limit_observed == capacity
            with pytest.raises(ResourceCeilingError, match="byte ceiling"):
                enforce_evaluator_tree_ceiling(launch)
    assert _host_private_mountpoint_identity() == host_tmp_identity


@pytest.mark.linux_isolation
def test_linux_evaluator_many_small_files_hit_the_physical_tmpfs_inode_envelope(
    tmp_path: Path,
) -> None:
    """A logically legal tree can still exhaust the tmpfs inode budget.

    ``EVALUATOR_DIRECTORY_ALLOWANCE`` is exactly the whole-tree directory bound plus the tmpfs
    root inode, so a maximally structured legal tree consumes the last inode without tripping any
    logical threshold or structural policy.  Only the physical inode envelope can stop it.
    """

    workspace = tmp_path / "workspace-inodes"
    oracle = tmp_path / "oracle-inodes"
    runtime = tmp_path / "runtime-inodes"
    workspace.mkdir()
    oracle.mkdir()
    _copy_fixture(runtime, "resource_probe.py")
    files = 20
    max_dirs = EVALUATOR_DIRECTORY_ALLOWANCE - 1
    ceilings = _ceilings(cpu=10, files=files, file_bytes=64 * 1024)
    host_tmp_identity = _host_private_mountpoint_identity()
    with _real_preflight(tmp_path) as preflight:
        with build_evaluator_sandbox(
            preflight,
            workspace=workspace,
            oracle=oracle,
            runtime=runtime,
            command=(
                "/usr/bin/python3",
                "-I",
                "/opt/measure-twice/runtime/resource_probe.py",
                "inodes",
                str(files),
                "1",
                str(max_dirs),
            ),
            ceilings=ceilings,
        ) as launch:
            result = _run_launch(launch, timeout=60)
            terminal_root = launch.terminal_tree_capability()
            values = os.fstatvfs(terminal_root.fd)
            assert values.f_ffree <= 0
            assert int(values.f_files) == ceilings.evaluator_tmpfs_inodes
            assert result.termination == "resource-limit"
            assert result.resource_limit == "file-count"
            assert result.resource_limit_provenance == "hard-guard"
            assert result.resource_limit_value == int(values.f_files)
            assert result.resource_limit_value > ceilings.evaluator_files
            assert result.resource_limit_observed == int(values.f_files)
            # The same retained identity Step 27 will snapshot, and a legal tree by every logical
            # measure: this hard guard is not a relabelled logical crossing.
            usage = walk_tree(terminal_root)
            assert usage.file_count == files
            assert usage.size_bytes < ceilings.evaluator_file_bytes
            assert usage.directory_count <= max_dirs
            assert int(values.f_bavail) > 0
    assert _host_private_mountpoint_identity() == host_tmp_identity


@pytest.mark.linux_isolation
def test_linux_terminal_validation_upgrades_a_sampled_threshold_with_the_physical_tmpfs_limit(
    tmp_path: Path,
) -> None:
    """Only a retained-FD exhaustion proof may overwrite a latched sampled threshold.

    ``_check_resources`` stops probing the moment ``limit_reached`` latches, so terminal
    validation is the sole parent-side path that can turn an already-recorded sampled crossing
    into ``hard-guard`` provenance carrying the physical tmpfs bound.
    """

    if sys.platform != "linux":
        pytest.skip("private tmpfs envelope canaries require Linux")
    workspace = tmp_path / "workspace-terminal-upgrade"
    oracle = tmp_path / "oracle-terminal-upgrade"
    runtime = tmp_path / "runtime-terminal-upgrade"
    seed = tmp_path / "seed-terminal-upgrade"
    workspace.mkdir()
    oracle.mkdir()
    seed.mkdir()
    _copy_fixture(runtime, "resource_probe.py")
    writers = 2
    page_size = int(os.sysconf("SC_PAGE_SIZE"))
    byte_limit = 1024 * 1024
    ceilings = _ceilings(
        cpu=10,
        files=writers,
        file_bytes=byte_limit,
        tmpfs_bytes=byte_limit + writers * page_size,
    )
    with _real_preflight(tmp_path) as preflight:
        with build_evaluator_sandbox(
            preflight,
            workspace=workspace,
            oracle=oracle,
            runtime=runtime,
            command=(
                "/usr/bin/python3",
                "-I",
                "/opt/measure-twice/runtime/resource_probe.py",
                "parallel-writers",
                str(writers),
                str(64 * 1024),
            ),
            ceilings=ceilings,
        ) as launch:
            _run_launch(launch, timeout=20)
            terminal_root = launch.terminal_tree_capability()
            values = os.fstatvfs(terminal_root.fd)
            assert values.f_bavail <= 0, "canary did not physically exhaust the private tmpfs"
            capacity = int(values.f_blocks) * int(values.f_frsize)
            scratch = EvaluatorScratch(
                source=LinuxPathCapability.acquire_absolute(seed, expected="directory"),
                file_limit=ceilings.evaluator_files,
                byte_limit=ceilings.evaluator_file_bytes,
                tmpfs_bytes=ceilings.evaluator_tmpfs_bytes,
                tmpfs_inodes=ceilings.evaluator_tmpfs_inodes,
            )
            try:
                scratch.record_backing_bounds(
                    bytes_capacity=capacity,
                    inode_capacity=int(values.f_files),
                )
                tracker = process_module._LinuxDescendantTracker(
                    root_pid=os.getpid(),
                    root_starttime=0,
                    resource_limits=ProcessResourceLimits(
                        tree_files=ceilings.evaluator_files,
                        tree_bytes=ceilings.evaluator_file_bytes,
                    ),
                    tree_capability=terminal_root,
                    evaluator_scratch=scratch,
                )
                # The garbage state this branch must overwrite: a latched sampled threshold that
                # reports the CONFIGURED ceiling and would survive if the retained-FD exhaustion
                # proof were dropped or downgraded to replace=False.
                tracker._set_resource_limit("file-count", observed=7)
                assert tracker.resource_limit_provenance == "sampled-threshold"
                assert tracker.resource_limit_value == ceilings.evaluator_files

                tracker.validate_terminal_tree()

                assert tracker.resource_limit == "file-bytes"
                assert tracker.resource_limit_provenance == "hard-guard"
                assert tracker.resource_limit_value == capacity
                assert tracker.resource_limit_observed == capacity
            finally:
                scratch.close()


@pytest.mark.linux_isolation
def test_linux_evaluator_elapsed_ms_measures_the_target_not_the_harness(
    tmp_path: Path,
) -> None:
    """``elapsed_ms`` spans the release barrier to target exit -- not bring-up, not teardown.

    This is the discriminating anchor for the three-phase clock, and it is load-bearing rather
    than cosmetic: Step 27 scores on this number, so charging harness time to the model is a
    corrupted measurement in exactly the sense ``measurement-validity.md`` forbids.

    The target is a no-op, so essentially ALL of the wall-clock time this call takes is harness
    time: systemd-run scope creation, cgroup delegation and readback, the bounded private tmpfs,
    the applied-tree copy, Bubblewrap, the FD handshake, then the teardown reap chain.  Anchoring
    the clock at the start of ``_start_process`` (or stopping it after ``_cleanup_process``) makes
    ``elapsed_ms`` approach the outer measurement; anchoring it at the release barrier and
    stopping it before teardown makes it a small fraction of it.
    """

    workspace = tmp_path / "workspace-elapsed"
    oracle = tmp_path / "oracle-elapsed"
    runtime = tmp_path / "runtime-elapsed"
    workspace.mkdir()
    oracle.mkdir()
    runtime.mkdir()
    with _real_preflight(tmp_path) as preflight:
        with build_evaluator_sandbox(
            preflight,
            workspace=workspace,
            oracle=oracle,
            runtime=runtime,
            command=(
                "/usr/bin/python3",
                "-I",
                "-c",
                f"import time; time.sleep({_TARGET_SLEEP_S})",
            ),
            ceilings=_ceilings(files=8),
        ) as launch:
            outer_started = time.monotonic()
            result = _run_launch(launch, timeout=20)
            outer_elapsed_ms = (time.monotonic() - outer_started) * 1000

    # Not vacuous: the target really ran and really exited cleanly.
    assert result.termination == "exited", result.stderr
    assert result.exit_code == 0
    # The target's own sleep is INSIDE the measurement.
    assert result.elapsed_ms >= _TARGET_SLEEP_S * 1000 * 0.9
    # ...and the harness bring-up and teardown are OUTSIDE it.  Measuring the excluded time
    # directly is what makes this robust: it does not assume the host is slow (an earlier
    # version asserted a floor on total wall-clock and failed on a warm machine that stood the
    # whole sandbox up in 98 ms), and it does not assume a ratio between two quantities that
    # scale differently.  Anchoring the clock before bring-up, or stopping it after the reap
    # chain, drives this difference to roughly zero.
    excluded_ms = outer_elapsed_ms - result.elapsed_ms
    assert excluded_ms >= 20, (
        f"elapsed_ms {result.elapsed_ms} is charging harness time against the target "
        f"(outer {outer_elapsed_ms:.0f} ms, excluded {excluded_ms:.0f} ms)"
    )
