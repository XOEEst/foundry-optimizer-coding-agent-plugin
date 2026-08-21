from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat
from typing import Iterator

from foundry_opt.bootstrap.errors import BootstrapApplyError

_LOCK_BYTE = b"\0"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@contextmanager
def state_file_lock(
    path: Path,
    *,
    locked_message: str,
) -> Iterator[None]:
    """Hold a non-blocking OS lock whose ownership dies with the process.

    The lock file is intentionally persistent; its existence never implies
    ownership.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise BootstrapApplyError(locked_message) from exc
    acquired = False
    try:
        _ensure_lock_byte(descriptor)
        _lock_descriptor(descriptor)
        acquired = True
    except OSError as exc:
        os.close(descriptor)
        raise BootstrapApplyError(locked_message) from exc
    try:
        yield
    finally:
        try:
            if acquired:
                _unlock_descriptor(descriptor)
        finally:
            os.close(descriptor)


def atomic_replace_state(
    path: Path,
    data: bytes,
    *,
    generation_hash: str,
) -> None:
    if _SHA256_RE.fullmatch(generation_hash) is None:
        raise BootstrapApplyError(
            "state generation hash is not a lowercase sha256 digest"
        )
    _remove_orphaned_temps(path)
    temp = path.with_name(f"{path.stem}.{generation_hash}.tmp")
    try:
        with open(temp, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        _sync_directory(path.parent)
    except Exception:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def _remove_orphaned_temps(path: Path) -> None:
    pattern = re.compile(
        rf"^{re.escape(path.stem)}\.[0-9a-f]{{64}}\.tmp$"
    )
    try:
        entries = tuple(path.parent.iterdir())
    except FileNotFoundError:
        return
    for candidate in entries:
        if pattern.fullmatch(candidate.name) is None:
            continue
        try:
            mode = candidate.stat(follow_symlinks=False).st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(mode):
            raise BootstrapApplyError(
                "orphaned state temporary path is not a regular file"
            )
        candidate.unlink()


def _ensure_lock_byte(descriptor: int) -> None:
    if os.fstat(descriptor).st_size != 0:
        return
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.write(descriptor, _LOCK_BYTE)
    os.fsync(descriptor)


def _lock_descriptor(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_descriptor(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["atomic_replace_state", "state_file_lock"]
