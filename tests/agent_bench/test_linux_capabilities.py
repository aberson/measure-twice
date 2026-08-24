"""Focused Linux-only coverage for descriptor-relative capability helpers."""

from __future__ import annotations

import array
import errno
import os
import socket
import stat
import sys
from pathlib import Path

import pytest

import measure_twice.agent_bench._linux_capabilities as capabilities_module
from measure_twice.agent_bench._linux_capabilities import (
    LinuxCapabilityError,
    LinuxPathCapability,
    LinuxTreeDriftError,
    LinuxTreeLimitError,
    LinuxTreePolicyError,
    copy_tree,
    open_verified_children,
    walk_tree,
)


def _require_linux() -> None:
    if sys.platform != "linux":
        pytest.skip("FD-relative tree copy requires Linux")


def _directory_capability(path: Path) -> LinuxPathCapability:
    return LinuxPathCapability.acquire_absolute(path, expected="directory")


def _exclusive_copy_destination(path: Path) -> LinuxPathCapability:
    capability = _directory_capability(path)
    capability._mark_exclusive_copy_destination()
    return capability


def _fd_snapshot() -> dict[int, tuple[int, int, int, str]]:
    """Capture owned-descriptor state without retaining procfs enumeration handles."""

    snapshot: dict[int, tuple[int, int, int, str]] = {}
    for raw_fd in os.listdir("/proc/self/fd"):
        fd = int(raw_fd)
        try:
            metadata = os.fstat(fd)
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        snapshot[fd] = (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode), target)
    return snapshot


@pytest.mark.linux_isolation
def test_copy_tree_preserves_contents_modes_and_root_ownership(tmp_path: Path) -> None:
    _require_linux()
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    nested = source / "nested"
    nested.mkdir(mode=0o750)
    nested.chmod(0o750)
    executable = nested / "run"
    executable.write_bytes(b"#!/bin/sh\necho copied\n")
    executable.chmod(0o751)
    plain = source / "plain.txt"
    plain.write_bytes(b"plain")
    plain.chmod(0o640)

    with (
        _directory_capability(source) as source_capability,
        _exclusive_copy_destination(destination) as destination_capability,
    ):
        usage = copy_tree(source_capability, destination_capability)
        assert usage.file_count == 2
        assert usage.size_bytes == len(executable.read_bytes()) + len(plain.read_bytes())
        assert usage.directory_count == 1
        assert source_capability.closed is False
        assert destination_capability.closed is False

    assert (destination / "nested" / "run").read_bytes() == executable.read_bytes()
    assert (destination / "plain.txt").read_bytes() == plain.read_bytes()
    assert stat.S_IMODE((destination / "nested").stat().st_mode) == 0o750
    assert stat.S_IMODE((destination / "nested" / "run").stat().st_mode) == 0o751
    assert stat.S_IMODE((destination / "plain.txt").stat().st_mode) == 0o640


@pytest.mark.linux_isolation
@pytest.mark.parametrize("special_kind", ["symlink", "fifo", "socket"])
def test_copy_tree_rejects_special_source_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    special_kind: str,
) -> None:
    _require_linux()
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    regular = source / "regular.txt"
    regular.write_bytes(b"regular")
    special = source / "special"
    server: socket.socket | None = None
    if special_kind == "symlink":
        special.symlink_to(regular)
    elif special_kind == "fifo":
        mkfifo = getattr(os, "mkfifo", None)
        if mkfifo is None:
            pytest.skip("FIFO fixture requires POSIX mkfifo support")
        mkfifo(special)
    else:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # AF_UNIX sun_path is capped near 108 bytes, which the staged WSL suite root can
        # exceed on its own.  Bind by bare name from inside the source directory so the
        # fixture depends on the entry, not on the absolute length of the test root.
        monkeypatch.chdir(source)
        server.bind(special.name)
    try:
        with (
            _directory_capability(source) as source_capability,
            _exclusive_copy_destination(destination) as destination_capability,
        ):
            with pytest.raises(LinuxTreePolicyError, match="symlink or special"):
                copy_tree(source_capability, destination_capability)
            assert source_capability.closed is False
            assert destination_capability.closed is False
    finally:
        if server is not None:
            server.close()


@pytest.mark.linux_isolation
# The surrogate literal is spelled out rather than computed: parametrize arguments are
# evaluated at import time, and os.fsdecode(b"...\xff") raises on Windows' utf-8/surrogatepass
# filesystem codec, which would break collection for the whole suite on a non-Linux host.
# On Linux it is byte-identical to os.fsdecode(b"nonutf-\xff").
@pytest.mark.parametrize("name", ["back\\slash", "nonutf-\udcff"])
def test_walk_tree_classifies_invalid_static_component_as_policy(
    tmp_path: Path,
    name: str,
) -> None:
    _require_linux()
    source = tmp_path / "source"
    source.mkdir()
    if "\udcff" in name:
        raw_path = os.fsencode(source) + b"/nonutf-\xff"
        fd = os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    else:
        (source / name).write_bytes(b"x")
    with _directory_capability(source) as source_capability:
        with pytest.raises(LinuxTreePolicyError, match="name violates tree policy"):
            walk_tree(source_capability)


@pytest.mark.linux_isolation
def test_walk_tree_classifies_stable_unreadable_and_structural_entries_as_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_linux()
    source = tmp_path / "source"
    source.mkdir()
    unreadable = source / "unreadable"
    unreadable.write_bytes(b"x")
    original_openat2 = capabilities_module._openat2

    def deny_unreadable(
        parent_fd: int,
        relative: str,
        *,
        flags: int,
        allow_symlinks: bool,
    ) -> int:
        if relative == "unreadable":
            raise capabilities_module._OpenAt2Error(errno.EACCES)
        return original_openat2(
            parent_fd,
            relative,
            flags=flags,
            allow_symlinks=allow_symlinks,
        )

    monkeypatch.setattr(capabilities_module, "_openat2", deny_unreadable)
    with _directory_capability(source) as source_capability:
        with pytest.raises(LinuxTreePolicyError, match="unreadable"):
            walk_tree(source_capability)

    monkeypatch.undo()
    (source / "second").write_bytes(b"y")
    monkeypatch.setattr(capabilities_module, "_MAX_DIRECTORY_ENTRIES", 1)
    with _directory_capability(source) as source_capability:
        with pytest.raises(LinuxTreePolicyError, match="structural entry bound"):
            walk_tree(source_capability)


@pytest.mark.linux_isolation
def test_copy_tree_enforces_limits_and_rejects_source_identity_changes(tmp_path: Path) -> None:
    _require_linux()
    source = tmp_path / "source"
    source.mkdir()
    first = source / "first.txt"
    first.write_bytes(b"first")
    second = source / "second.txt"
    second.write_bytes(b"second")
    limited_destination = tmp_path / "limited-destination"
    limited_destination.mkdir()

    with (
        _directory_capability(source) as source_capability,
        _exclusive_copy_destination(limited_destination) as destination_capability,
    ):
        with pytest.raises(LinuxTreeLimitError) as error:
            copy_tree(source_capability, destination_capability, file_limit=1)
        assert error.value.limit_name == "file-count"
        assert source_capability.closed is False
        assert destination_capability.closed is False

    byte_limited_destination = tmp_path / "byte-limited-destination"
    byte_limited_destination.mkdir()
    with (
        _directory_capability(source) as source_capability,
        _exclusive_copy_destination(byte_limited_destination) as destination_capability,
    ):
        with pytest.raises(LinuxTreeLimitError) as error:
            copy_tree(source_capability, destination_capability, byte_limit=1)
        assert error.value.limit_name == "file-bytes"

    raced_destination = tmp_path / "raced-destination"
    raced_destination.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside-canary")
    moved = source / "moved.txt"

    def replace_first(relative: str) -> None:
        if relative == "first.txt":
            first.rename(moved)
            first.symlink_to(outside)

    with (
        _directory_capability(source) as source_capability,
        _exclusive_copy_destination(raced_destination) as destination_capability,
    ):
        with pytest.raises(LinuxTreeDriftError, match="changed after acquisition"):
            copy_tree(source_capability, destination_capability, before_open=replace_first)
        assert not (raced_destination / "first.txt").exists()
        assert outside.read_bytes() == b"outside-canary"

    nonempty_destination = tmp_path / "nonempty-destination"
    nonempty_destination.mkdir()
    (nonempty_destination / "existing.txt").write_bytes(b"existing")
    with (
        _directory_capability(source) as source_capability,
        _exclusive_copy_destination(nonempty_destination) as destination_capability,
    ):
        with pytest.raises(LinuxCapabilityError, match="destination must be empty"):
            copy_tree(source_capability, destination_capability)


@pytest.mark.linux_isolation
def test_copy_tree_rejects_nonexclusive_destination_capability(tmp_path: Path) -> None:
    _require_linux()
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "seed.txt").write_bytes(b"seed")

    with (
        _directory_capability(source) as source_capability,
        _directory_capability(destination) as destination_capability,
    ):
        with pytest.raises(LinuxCapabilityError, match="exclusive private capability"):
            copy_tree(source_capability, destination_capability)
    assert list(destination.iterdir()) == []


@pytest.mark.linux_isolation
def test_scm_rights_capability_adoption_forces_cloexec_and_releases_fd(
    tmp_path: Path,
) -> None:
    """SCM_RIGHTS clears CLOEXEC unless the receiver's capability boundary restores it."""

    _require_linux()
    baseline = _fd_snapshot()
    directory = tmp_path / "received-directory"
    directory.mkdir()
    source_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    received_fd = -1
    capability: LinuxPathCapability | None = None
    try:
        sender.sendmsg(
            [b"F"],
            [
                (
                    socket.SOL_SOCKET,
                    socket.SCM_RIGHTS,
                    array.array("i", [source_fd]),
                )
            ],
        )
        payload, ancillary, _flags, _address = receiver.recvmsg(
            1,
            socket.CMSG_SPACE(array.array("i").itemsize),
        )
        assert payload == b"F"
        received: list[int] = []
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                values = array.array("i")
                values.frombytes(data[: len(data) - (len(data) % values.itemsize)])
                received.extend(values)
        assert len(received) == 1
        received_fd = received[0]
        # The call intentionally omitted MSG_CMSG_CLOEXEC. Linux hands the receiver an
        # inheritable descriptor; LinuxPathCapability is the boundary that repairs it.
        assert os.get_inheritable(received_fd) is True
        capability = LinuxPathCapability._from_open_fd(
            received_fd,
            display_path="/received-directory",
            expected="directory",
        )
        received_fd = -1
        assert os.get_inheritable(capability.fd) is False
        owned_fd = capability.fd
        capability.close()
        capability = None
        with pytest.raises(OSError):
            os.fstat(owned_fd)

        regular = directory / "not-a-directory"
        regular.write_bytes(b"x")
        rejected_fd = os.open(regular, os.O_RDONLY | os.O_CLOEXEC)
        with pytest.raises(LinuxCapabilityError):
            LinuxPathCapability._from_open_fd(
                rejected_fd,
                display_path="/not-a-directory",
                expected="directory",
            )
        with pytest.raises(OSError):
            os.fstat(rejected_fd)
    finally:
        if capability is not None:
            capability.close()
        if received_fd >= 0:
            os.close(received_fd)
        sender.close()
        receiver.close()
        os.close(source_fd)
    assert _fd_snapshot() == baseline


@pytest.mark.linux_isolation
@pytest.mark.parametrize("operation", ["copy", "walk", "children"])
def test_held_child_acquisition_rejects_unlink_recreate_aba(
    tmp_path: Path,
    operation: str,
) -> None:
    _require_linux()
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    victim = source / "victim.txt"
    victim.write_bytes(b"GOOD")
    victim.chmod(0o640)

    def recreate(relative: str) -> None:
        if relative != "victim.txt":
            return
        victim.unlink()
        victim.write_bytes(b"EVIL")
        victim.chmod(0o640)

    with _directory_capability(source) as source_capability:
        if operation == "walk":
            with pytest.raises(LinuxTreeDriftError, match="changed after acquisition"):
                walk_tree(source_capability, before_open=recreate)
        elif operation == "children":
            with pytest.raises(LinuxTreeDriftError, match="changed after acquisition"):
                open_verified_children(source_capability, before_open=recreate)
        else:
            with _exclusive_copy_destination(destination) as destination_capability:
                with pytest.raises(LinuxTreeDriftError, match="changed after acquisition"):
                    copy_tree(
                        source_capability,
                        destination_capability,
                        before_open=recreate,
                    )
    assert victim.read_bytes() == b"EVIL"
    assert list(destination.iterdir()) == []


@pytest.mark.linux_isolation
def test_pre_acquisition_replacement_never_authorizes_stale_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-open swap is either rejected by directory coherence or acquired as current."""

    _require_linux()
    source = tmp_path / "source"
    source.mkdir()
    victim = source / "victim.txt"
    victim.write_bytes(b"GOOD")
    victim.chmod(0o640)
    original_openat2 = capabilities_module._openat2
    replaced = False

    def replace_before_open(
        parent_fd: int,
        relative: str,
        *,
        flags: int,
        allow_symlinks: bool,
    ) -> int:
        nonlocal replaced
        if relative == "victim.txt" and not replaced:
            replaced = True
            victim.unlink()
            victim.write_bytes(b"EVIL")
            victim.chmod(0o640)
        return original_openat2(
            parent_fd,
            relative,
            flags=flags,
            allow_symlinks=allow_symlinks,
        )

    monkeypatch.setattr(capabilities_module, "_openat2", replace_before_open)
    with _directory_capability(source) as source_capability:
        try:
            children = open_verified_children(source_capability)
        except LinuxTreeDriftError as exc:
            # A source-directory metadata change is independently fail-closed.  The acquisition
            # boundary must not promise success when that broader snapshot contract rejects it.
            assert "source directory changed" in str(exc) or "entries changed" in str(exc)
            children = ()
        try:
            assert replaced is True
            if children:
                assert len(children) == 1
                assert children[0][0] == "victim.txt"
                # If the surrounding directory remained coherent enough to complete, the held
                # no-follow descriptor is the object selected at open time, never stale GOOD.
                assert os.read(children[0][1].fd, 4) == b"EVIL"
        finally:
            for _name, capability in children:
                capability.close()


@pytest.mark.linux_isolation
def test_copy_tree_rejects_same_size_mid_read_source_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_linux()
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    payload = b"A" * (1024 * 1024 + 32)
    source_file = source / "large.txt"
    source_file.write_bytes(payload)
    source_file.chmod(0o640)
    original_read = os.read
    mutated = False

    def mutate_during_copy(fd: int, count: int) -> bytes:
        nonlocal mutated
        block = original_read(fd, count)
        if not mutated and block == payload[: 1024 * 1024]:
            source_file.write_bytes(b"B" * len(payload))
            source_file.chmod(0o640)
            mutated = True
        return block

    monkeypatch.setattr(os, "read", mutate_during_copy)
    with (
        _directory_capability(source) as source_capability,
        _exclusive_copy_destination(destination) as destination_capability,
    ):
        with pytest.raises(LinuxTreeDriftError, match="source file changed while being copied"):
            copy_tree(source_capability, destination_capability)
    assert mutated is True
    assert source_file.read_bytes() == b"B" * len(payload)


@pytest.mark.linux_isolation
@pytest.mark.parametrize("change", ["add", "remove", "rename"])
def test_copy_tree_rejects_late_source_namespace_changes(
    tmp_path: Path,
    change: str,
) -> None:
    _require_linux()
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    first = source / "first.txt"
    second = source / "second.txt"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    def mutate_namespace(relative: str) -> None:
        if relative != "first.txt":
            return
        if change == "add":
            (source / "late.txt").write_bytes(b"late")
        elif change == "remove":
            second.unlink()
        else:
            second.rename(source / "renamed.txt")

    with (
        _directory_capability(source) as source_capability,
        _exclusive_copy_destination(destination) as destination_capability,
    ):
        with pytest.raises(LinuxTreeDriftError, match=r"source directory|FD-relative entry"):
            copy_tree(
                source_capability,
                destination_capability,
                before_open=mutate_namespace,
            )
