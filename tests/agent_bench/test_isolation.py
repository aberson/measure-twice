from __future__ import annotations

import copy
import errno
import itertools
import json
import os
import pickle
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
from measure_twice.agent_bench.models import Ceilings, load_execution_profile
from measure_twice.agent_bench.process import (
    EVALUATOR_WORKSPACE_FD_TOKEN,
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
_FAKE_ROOT = PurePosixPath("/var/lib/measure-twice-test")


def _ceilings(
    *,
    cpu: int = 2,
    memory: int = 256 * 1024 * 1024,
    processes: int = 16,
    files: int = 100,
    file_bytes: int = 1024 * 1024,
    cpu_bandwidth_percent: int = 100,
    tmpfs_bytes: int | None = None,
    tmpfs_inodes: int | None = None,
) -> Ceilings:
    bounded_tmpfs_bytes = max(file_bytes + files * 4096, 2 * 1024 * 1024)
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
        evaluator_tmpfs_inodes=(files + 10_001 if tmpfs_inodes is None else tmpfs_inodes),
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
@pytest.mark.parametrize("operation", ["cpu", "memory", "processes"])
def test_linux_evaluator_aggregate_cpu_memory_and_process_limits(
    tmp_path: Path,
    operation: str,
) -> None:
    workspace = tmp_path / f"workspace-{operation}"
    oracle = tmp_path / f"oracle-{operation}"
    runtime = tmp_path / f"runtime-{operation}"
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
                operation,
            ),
            ceilings=_ceilings(
                cpu=1 if operation == "cpu" else 10,
                memory=128 * 1024 * 1024,
                processes=8,
            ),
        ) as launch:
            result = _run_launch(launch, timeout=5)
    assert result.termination == "resource-limit"
    assert result.resource_limit == operation


@pytest.mark.linux_isolation
@pytest.mark.parametrize(
    ("count", "size", "files", "file_bytes", "live_limit", "message"),
    [
        (3, 10, 2, 100, "file-count", "file ceiling"),
        (2, 80, 10, 128, "file-bytes", "byte ceiling"),
    ],
)
def test_linux_evaluator_terminal_file_ceilings(
    tmp_path: Path,
    count: int,
    size: int,
    files: int,
    file_bytes: int,
    live_limit: str,
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
            result = _run_launch(launch)
            assert result.termination == "resource-limit"
            assert result.exit_code is None
            assert result.resource_limit == live_limit
            with pytest.raises(ResourceCeilingError, match=message):
                enforce_evaluator_tree_ceiling(launch)
