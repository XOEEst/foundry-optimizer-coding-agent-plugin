from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import (
    BinaryIO,
    Callable,
    ContextManager,
    Final,
    Iterable,
    Iterator,
    Sequence,
)


NORMALIZED_TIMESTAMP: Final = (1980, 1, 1, 0, 0, 0)
NORMALIZED_FILE_MODE: Final = 0o644
DEFAULT_MAX_ARCHIVE_BYTES: Final = 250 * 1024 * 1024
DEFAULT_MAX_MEMBER_COUNT: Final = 10_000
DEFAULT_MAX_MEMBER_UNCOMPRESSED_BYTES: Final = 250 * 1024 * 1024
DEFAULT_MAX_TOTAL_UNCOMPRESSED_BYTES: Final = 500 * 1024 * 1024
DEFAULT_MAX_EXPANSION_RATIO: Final = 1_000.0
DEFAULT_MAX_TOTAL_EXPANSION_RATIO: Final = 1_000.0
_READ_CHUNK_BYTES: Final = 1024 * 1024
_EOCD_SIGNATURE: Final = b"PK\x05\x06"
_ZIP64_EOCD_SIGNATURE: Final = b"PK\x06\x06"
_ZIP64_LOCATOR_SIGNATURE: Final = b"PK\x06\x07"
_CENTRAL_DIRECTORY_SIGNATURE: Final = b"PK\x01\x02"
_LOCAL_FILE_HEADER_SIGNATURE: Final = b"PK\x03\x04"
_EOCD_SIZE: Final = 22
_ZIP64_LOCATOR_SIZE: Final = 20
_ZIP64_EOCD_MIN_SIZE: Final = 56
_CENTRAL_DIRECTORY_HEADER_SIZE: Final = 46
_LOCAL_FILE_HEADER_SIZE: Final = 30
_TREE_DOMAIN: Final = b"foundry-opt-tree-v1\0"
_GLOB_CHARS: Final = frozenset("*?")
DeadlineCheck = Callable[[], None]
VerifiedSourceOpener = Callable[[str], ContextManager[BinaryIO]]


class _DeadlineGuard:
    def __init__(self, callback: DeadlineCheck) -> None:
        self._callback = callback
        self.failure: BaseException | None = None

    def __call__(self) -> None:
        if self.failure is not None:
            return
        try:
            self._callback()
        except BaseException as exc:
            self.failure = exc
            raise

    def raise_if_failed(self) -> None:
        if self.failure is not None:
            raise self.failure


class PackagingError(ValueError):
    """Base error for deterministic source packaging."""


class UnsafeSourcePathError(PackagingError):
    """A source path, pattern, or filesystem entry is unsafe."""


class UnsafeArchiveError(PackagingError):
    """An existing ZIP does not satisfy the deterministic archive contract."""


@dataclass(frozen=True, slots=True)
class TreeEntry:
    path: str
    size: int
    sha256: str
    mode: int = NORMALIZED_FILE_MODE


@dataclass(frozen=True, slots=True)
class TreeFingerprint:
    entries: tuple[TreeEntry, ...]
    tree_sha256: str

    @property
    def files(self) -> tuple[str, ...]:
        return tuple(entry.path for entry in self.entries)


@dataclass(frozen=True, slots=True)
class DeterministicZipResult:
    zip_path: Path
    entries: tuple[TreeEntry, ...]
    tree_sha256: str
    zip_sha256: str
    size_bytes: int

    @property
    def files(self) -> tuple[str, ...]:
        return tuple(entry.path for entry in self.entries)


@dataclass(frozen=True, slots=True)
class _SelectedFile:
    entry: TreeEntry
    source_path: Path
    device: int
    inode: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class _PathMatcher:
    source: str
    regex: re.Pattern[str]

    def matches(self, value: str) -> bool:
        return self.regex.fullmatch(value) is not None


class _CheckedBinaryReader:
    def __init__(
        self,
        stream: BinaryIO,
        *,
        check_deadline: DeadlineCheck,
        maximum_bytes: int,
    ) -> None:
        self._stream = stream
        self._check_deadline = check_deadline
        self._maximum_bytes = maximum_bytes

    def read(self, size: int = -1) -> bytes:
        self._check_deadline()
        chunks: list[bytes] = []
        total = 0
        while size < 0 or total < size:
            requested = _READ_CHUNK_BYTES
            if size >= 0:
                requested = min(requested, size - total)
            if requested <= 0:
                break
            self._check_deadline()
            chunk = self._stream.read(requested)
            self._check_deadline()
            if not chunk:
                break
            total += len(chunk)
            if total > self._maximum_bytes:
                raise UnsafeArchiveError(
                    "ZIP read exceeds the configured archive byte limit"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        self._check_deadline()
        result = self._stream.seek(offset, whence)
        self._check_deadline()
        return result

    def tell(self) -> int:
        self._check_deadline()
        return self._stream.tell()

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    @property
    def closed(self) -> bool:
        return self._stream.closed


class _CheckedBinaryWriter:
    def __init__(
        self,
        stream: BinaryIO,
        *,
        check_deadline: DeadlineCheck,
        maximum_bytes: int,
    ) -> None:
        self._stream = stream
        self._check_deadline = check_deadline
        self._maximum_bytes = maximum_bytes
        self._maximum_position = 0

    def write(self, payload: bytes) -> int:
        self._check_deadline()
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            self._check_deadline()
            written = self._stream.write(
                view[offset : offset + _READ_CHUNK_BYTES]
            )
            self._check_deadline()
            if written is None or written <= 0:
                raise UnsafeArchiveError(
                    "canonical ZIP write made no progress"
                )
            offset += written
            self._maximum_position = max(
                self._maximum_position,
                self._stream.tell(),
            )
            if self._maximum_position > self._maximum_bytes:
                raise UnsafeArchiveError(
                    "canonical ZIP exceeds the configured archive byte limit"
                )
        return offset

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        self._check_deadline()
        result = self._stream.seek(offset, whence)
        self._check_deadline()
        return result

    def tell(self) -> int:
        self._check_deadline()
        return self._stream.tell()

    def flush(self) -> None:
        self._check_deadline()
        self._stream.flush()
        self._check_deadline()

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return True

    @property
    def closed(self) -> bool:
        return self._stream.closed


class _ComparingBinaryWriter:
    def __init__(
        self,
        source: _CheckedBinaryReader,
        *,
        archive_size: int,
        check_deadline: DeadlineCheck,
    ) -> None:
        self._source = source
        self._archive_size = archive_size
        self._check_deadline = check_deadline
        self._position = 0
        self._maximum_position = 0
        self._pending_local_headers: set[tuple[int, int]] = set()
        self._aborting = False

    def write(self, payload: bytes) -> int:
        self._check_deadline()
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            self._check_deadline()
            chunk = bytes(view[offset : offset + _READ_CHUNK_BYTES])
            if (
                self._aborting
                or (
                isinstance(self._check_deadline, _DeadlineGuard)
                and self._check_deadline.failure is not None
                )
            ):
                self._aborting = True
                self._position += len(chunk)
                self._maximum_position = max(
                    self._maximum_position,
                    self._position,
                )
                offset += len(chunk)
                continue
            if (
                self._position > self._archive_size
                or len(chunk) > self._archive_size - self._position
            ):
                raise UnsafeArchiveError(
                    "canonical ZIP exceeds the source archive"
                )
            self._source.seek(self._position)
            expected = self._source.read(len(chunk))
            self._check_deadline()
            header_key = (self._position, len(chunk))
            if expected == chunk:
                self._pending_local_headers.discard(header_key)
            elif (
                chunk.startswith(_LOCAL_FILE_HEADER_SIGNATURE)
                and _LOCAL_FILE_HEADER_SIZE
                <= len(chunk)
                <= _LOCAL_FILE_HEADER_SIZE + (2 * 0xFFFF)
            ):
                self._pending_local_headers.add(header_key)
                if len(self._pending_local_headers) > DEFAULT_MAX_MEMBER_COUNT:
                    raise UnsafeArchiveError(
                        "canonical ZIP has too many pending local headers"
                    )
            elif header_key not in self._pending_local_headers:
                raise UnsafeArchiveError(
                    "ZIP bytes do not match the canonical deterministic "
                    "serialization"
                )
            self._position += len(chunk)
            self._maximum_position = max(
                self._maximum_position,
                self._position,
            )
            offset += len(chunk)
        return offset

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        self._check_deadline()
        if whence == os.SEEK_SET:
            position = offset
        elif whence == os.SEEK_CUR:
            position = self._position + offset
        elif whence == os.SEEK_END:
            position = self._archive_size + offset
        else:
            raise ValueError("unsupported seek mode")
        if (
            not self._aborting
            and (position < 0 or position > self._archive_size)
        ):
            raise UnsafeArchiveError("canonical ZIP seek is out of bounds")
        self._position = max(0, position)
        self._check_deadline()
        return position

    def tell(self) -> int:
        self._check_deadline()
        return self._position

    def flush(self) -> None:
        self._check_deadline()

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return True

    @property
    def closed(self) -> bool:
        return False

    def verify_complete(self) -> None:
        self._check_deadline()
        if (
            self._position != self._archive_size
            or self._maximum_position != self._archive_size
            or self._pending_local_headers
        ):
            raise UnsafeArchiveError(
                "canonical ZIP size does not match the source archive"
            )

    def abort(self) -> None:
        self._aborting = True


class _AbortComparisonOnError:
    def __init__(self, writer: _ComparingBinaryWriter) -> None:
        self._writer = writer

    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object,
    ) -> bool:
        del exception, traceback
        if exception_type is not None:
            self._writer.abort()
        return False


class _DeadlineCheckedList(list[zipfile.ZipInfo]):
    def __init__(
        self,
        values: Iterable[zipfile.ZipInfo],
        check_deadline: DeadlineCheck,
    ) -> None:
        super().__init__(values)
        self._check_deadline = check_deadline

    def __iter__(self) -> Iterator[zipfile.ZipInfo]:
        for value in super().__iter__():
            self._check_deadline()
            yield value


class DeterministicZipBuilder:
    """Build a byte-for-byte stable ZIP from an explicitly selected source tree."""

    def __init__(
        self,
        *,
        includes: Sequence[str],
        excludes: Sequence[str] = (),
        timestamp: tuple[int, int, int, int, int, int] = NORMALIZED_TIMESTAMP,
        file_mode: int = NORMALIZED_FILE_MODE,
        compresslevel: int = 9,
        max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
        max_member_count: int = DEFAULT_MAX_MEMBER_COUNT,
        max_member_uncompressed_bytes: int = (
            DEFAULT_MAX_MEMBER_UNCOMPRESSED_BYTES
        ),
        max_total_uncompressed_bytes: int = (
            DEFAULT_MAX_TOTAL_UNCOMPRESSED_BYTES
        ),
    ) -> None:
        if not includes:
            raise PackagingError("at least one include pattern is required")
        if timestamp < NORMALIZED_TIMESTAMP:
            raise PackagingError("ZIP timestamps cannot predate 1980-01-01")
        if file_mode < 0 or file_mode > 0o777:
            raise PackagingError("file_mode must be a POSIX permission value")
        if compresslevel < 0 or compresslevel > 9:
            raise PackagingError("compresslevel must be between 0 and 9")
        for name, value in {
            "max_archive_bytes": max_archive_bytes,
            "max_member_count": max_member_count,
            "max_member_uncompressed_bytes": (
                max_member_uncompressed_bytes
            ),
            "max_total_uncompressed_bytes": (
                max_total_uncompressed_bytes
            ),
        }.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
            ):
                raise PackagingError(f"{name} must be a positive integer")

        self._includes = tuple(_compile_pattern(value) for value in includes)
        self._excludes = tuple(_compile_pattern(value) for value in excludes)
        self._timestamp = timestamp
        self._file_mode = file_mode
        self._compresslevel = compresslevel
        self._max_archive_bytes = max_archive_bytes
        self._max_member_count = max_member_count
        self._max_member_uncompressed_bytes = (
            max_member_uncompressed_bytes
        )
        self._max_total_uncompressed_bytes = (
            max_total_uncompressed_bytes
        )

    def fingerprint(
        self,
        source_root: Path,
        *,
        ignored_output: Path | None = None,
        check_deadline: DeadlineCheck,
    ) -> TreeFingerprint:
        deadline_guard = _DeadlineGuard(check_deadline)
        check_deadline = deadline_guard
        check_deadline()
        selected = self._select(
            source_root,
            ignored_output=ignored_output,
            check_deadline=check_deadline,
        )
        entries_list: list[TreeEntry] = []
        for item in selected:
            check_deadline()
            entries_list.append(item.entry)
        entries = tuple(entries_list)
        result = TreeFingerprint(
            entries=entries,
            tree_sha256=_hash_tree(entries, check_deadline=check_deadline),
        )
        deadline_guard.raise_if_failed()
        return result

    def build(
        self,
        source_root: Path,
        destination: Path,
        *,
        check_deadline: DeadlineCheck,
    ) -> DeterministicZipResult:
        deadline_guard = _DeadlineGuard(check_deadline)
        check_deadline = deadline_guard
        check_deadline()
        source_root = Path(source_root)
        destination = Path(destination)
        _validate_output_path(destination)
        selected = self._select(
            source_root,
            ignored_output=destination,
            check_deadline=check_deadline,
        )
        entries_list: list[TreeEntry] = []
        for item in selected:
            check_deadline()
            entries_list.append(item.entry)
        entries = tuple(entries_list)

        check_deadline()
        destination.parent.mkdir(parents=True, exist_ok=True)
        _serialize_selected(
            selected,
            destination=destination,
            timestamp=self._timestamp,
            file_mode=self._file_mode,
            compresslevel=self._compresslevel,
            check_deadline=check_deadline,
            max_archive_bytes=self._max_archive_bytes,
        )
        check_deadline()
        zip_sha256, size_bytes = _hash_file(
            destination,
            check_deadline=check_deadline,
            maximum_bytes=self._max_archive_bytes,
        )
        result = DeterministicZipResult(
            zip_path=destination,
            entries=entries,
            tree_sha256=_hash_tree(
                entries,
                check_deadline=check_deadline,
            ),
            zip_sha256=zip_sha256,
            size_bytes=size_bytes,
        )
        deadline_guard.raise_if_failed()
        return result

    def fingerprint_entries(
        self,
        entries: Sequence[TreeEntry],
        *,
        check_deadline: DeadlineCheck,
    ) -> TreeFingerprint:
        deadline_guard = _DeadlineGuard(check_deadline)
        check_deadline = deadline_guard
        check_deadline()
        selected = self._select_entries(
            entries,
            check_deadline=check_deadline,
        )
        result = TreeFingerprint(
            entries=selected,
            tree_sha256=_hash_tree(
                selected,
                check_deadline=check_deadline,
            ),
        )
        deadline_guard.raise_if_failed()
        return result

    def build_entries(
        self,
        entries: Sequence[TreeEntry],
        destination: Path,
        *,
        open_source: VerifiedSourceOpener,
        check_deadline: DeadlineCheck,
    ) -> DeterministicZipResult:
        deadline_guard = _DeadlineGuard(check_deadline)
        check_deadline = deadline_guard
        check_deadline()
        if not callable(open_source):
            raise PackagingError("open_source must be callable")
        destination = Path(destination)
        _validate_output_path(destination)
        selected = self._select_entries(
            entries,
            check_deadline=check_deadline,
        )
        check_deadline()
        destination.parent.mkdir(parents=True, exist_ok=True)
        _serialize_verified_entries(
            selected,
            destination=destination,
            open_source=open_source,
            timestamp=self._timestamp,
            file_mode=self._file_mode,
            compresslevel=self._compresslevel,
            check_deadline=check_deadline,
            max_archive_bytes=self._max_archive_bytes,
        )
        check_deadline()
        zip_sha256, size_bytes = _hash_file(
            destination,
            check_deadline=check_deadline,
            maximum_bytes=self._max_archive_bytes,
        )
        result = DeterministicZipResult(
            zip_path=destination,
            entries=selected,
            tree_sha256=_hash_tree(
                selected,
                check_deadline=check_deadline,
            ),
            zip_sha256=zip_sha256,
            size_bytes=size_bytes,
        )
        deadline_guard.raise_if_failed()
        return result

    def _select_entries(
        self,
        entries: Sequence[TreeEntry],
        *,
        check_deadline: DeadlineCheck,
    ) -> tuple[TreeEntry, ...]:
        if isinstance(entries, (str, bytes)):
            raise PackagingError("verified source entries must be a sequence")
        selected: list[TreeEntry] = []
        seen_casefolded: dict[str, str] = {}
        selected_bytes = 0
        for index, entry in enumerate(entries, start=1):
            check_deadline()
            if index > self._max_member_count:
                raise UnsafeArchiveError(
                    "source tree exceeds the configured member count limit"
                )
            if not isinstance(entry, TreeEntry):
                raise PackagingError(
                    "verified source entries must contain TreeEntry values"
                )
            _validate_member_path(
                entry.path,
                error_type=UnsafeSourcePathError,
            )
            if (
                isinstance(entry.size, bool)
                or not isinstance(entry.size, int)
                or entry.size < 0
                or entry.size > self._max_member_uncompressed_bytes
            ):
                raise UnsafeArchiveError(
                    "verified source entry has an invalid size: "
                    f"{entry.path}"
                )
            if re.fullmatch(r"[0-9a-f]{64}", entry.sha256) is None:
                raise PackagingError(
                    "verified source entry has an invalid SHA-256: "
                    f"{entry.path}"
                )
            if entry.mode != self._file_mode:
                raise PackagingError(
                    "verified source entry has a non-canonical mode: "
                    f"{entry.path}"
                )
            casefolded = entry.path.casefold()
            previous = seen_casefolded.get(casefolded)
            if previous is not None:
                raise UnsafeSourcePathError(
                    "case-insensitive archive path collision: "
                    f"{previous}, {entry.path}"
                )
            seen_casefolded[casefolded] = entry.path
            if not self._is_selected(entry.path):
                continue
            selected_bytes += entry.size
            if selected_bytes > self._max_total_uncompressed_bytes:
                raise UnsafeArchiveError(
                    "ZIP exceeds the configured total uncompressed byte limit"
                )
            selected.append(entry)
        check_deadline()
        selected.sort(
            key=lambda entry: (
                check_deadline(),
                entry.path,
            )[1]
        )
        check_deadline()
        if not selected:
            includes = ", ".join(matcher.source for matcher in self._includes)
            raise PackagingError(f"include patterns selected no files: {includes}")
        return tuple(selected)

    def _select(
        self,
        source_root: Path,
        *,
        ignored_output: Path | None,
        check_deadline: DeadlineCheck,
    ) -> tuple[_SelectedFile, ...]:
        check_deadline()
        source_root = Path(source_root)
        root_stat = source_root.lstat()
        if source_root.is_symlink():
            raise UnsafeSourcePathError(f"source root is a symlink: {source_root}")
        if not stat.S_ISDIR(root_stat.st_mode):
            raise PackagingError(f"source root is not a directory: {source_root}")

        root_absolute = source_root.absolute()
        ignored_absolute = ignored_output.absolute() if ignored_output is not None else None
        selected: list[_SelectedFile] = []
        seen_casefolded: dict[str, str] = {}
        selected_bytes = 0
        enumerated_paths = 0

        def walk(directory: Path, relative_parts: tuple[str, ...]) -> None:
            nonlocal enumerated_paths, selected_bytes
            check_deadline()
            try:
                def checked_name(item: os.DirEntry[str]) -> str:
                    check_deadline()
                    return item.name

                children: list[os.DirEntry[str]] = []
                with os.scandir(directory) as scanner:
                    while True:
                        check_deadline()
                        try:
                            child = next(scanner)
                        except StopIteration:
                            break
                        check_deadline()
                        enumerated_paths += 1
                        if enumerated_paths > self._max_member_count:
                            raise UnsafeArchiveError(
                                "source tree exceeds the configured "
                                "member count limit"
                            )
                        children.append(child)
                check_deadline()
                children.sort(key=checked_name)
                check_deadline()
            except OSError as exc:
                raise PackagingError(f"cannot read source directory: {directory}") from exc

            for child in children:
                check_deadline()
                child_path = Path(child.path)
                relative_path = "/".join((*relative_parts, child.name))
                _validate_member_path(relative_path, error_type=UnsafeSourcePathError)
                try:
                    child_stat = child.stat(follow_symlinks=False)
                except OSError as exc:
                    raise PackagingError(f"cannot stat source path: {child_path}") from exc

                if stat.S_ISLNK(child_stat.st_mode):
                    raise UnsafeSourcePathError(
                        f"symlinks are not permitted in source packages: {relative_path}"
                    )
                if stat.S_ISDIR(child_stat.st_mode):
                    walk(child_path, (*relative_parts, child.name))
                    continue
                if not stat.S_ISREG(child_stat.st_mode):
                    raise UnsafeSourcePathError(
                        f"special filesystem entries are not permitted: {relative_path}"
                    )
                if ignored_absolute is not None and child_path.absolute() == ignored_absolute:
                    continue
                if not self._is_selected(relative_path):
                    continue

                casefolded = relative_path.casefold()
                previous = seen_casefolded.get(casefolded)
                if previous is not None and previous != relative_path:
                    raise UnsafeSourcePathError(
                        f"case-insensitive archive path collision: {previous}, {relative_path}"
                    )
                seen_casefolded[casefolded] = relative_path

                if len(selected) >= self._max_member_count:
                    raise UnsafeArchiveError(
                        "ZIP exceeds the configured member count limit"
                    )
                check_deadline()
                try:
                    flags = os.O_RDONLY
                    if hasattr(os, "O_BINARY"):
                        flags |= os.O_BINARY
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    descriptor = os.open(child_path, flags)
                    with os.fdopen(descriptor, "rb") as stream:
                        opened_stat = os.fstat(stream.fileno())
                        if (
                            not stat.S_ISREG(opened_stat.st_mode)
                            or (
                                child_stat.st_dev != 0
                                and child_stat.st_ino != 0
                                and not os.path.samestat(
                                    opened_stat,
                                    child_stat,
                                )
                            )
                        ):
                            raise UnsafeSourcePathError(
                                "source changed while packaging: "
                                f"{relative_path}"
                            )
                        digest = hashlib.sha256()
                        size = 0
                        while True:
                            check_deadline()
                            chunk = stream.read(_READ_CHUNK_BYTES)
                            check_deadline()
                            if not chunk:
                                break
                            size += len(chunk)
                            if (
                                size
                                > self._max_member_uncompressed_bytes
                            ):
                                raise UnsafeArchiveError(
                                    "ZIP member exceeds the configured "
                                    "per-member byte limit: "
                                    f"{relative_path}"
                                )
                            if (
                                selected_bytes + size
                                > self._max_total_uncompressed_bytes
                            ):
                                raise UnsafeArchiveError(
                                    "ZIP exceeds the configured total "
                                    "uncompressed byte limit"
                                )
                            digest.update(chunk)
                        closed_stat = os.fstat(stream.fileno())
                        if (
                            (
                                closed_stat.st_dev,
                                closed_stat.st_ino,
                            )
                            != (
                                opened_stat.st_dev,
                                opened_stat.st_ino,
                            )
                            or closed_stat.st_size != size
                            or closed_stat.st_mtime_ns
                            != opened_stat.st_mtime_ns
                        ):
                            raise UnsafeSourcePathError(
                                "source changed while packaging: "
                                f"{relative_path}"
                            )
                except OSError as exc:
                    raise PackagingError(f"cannot read source file: {child_path}") from exc

                selected.append(
                    _SelectedFile(
                        entry=TreeEntry(
                            path=relative_path,
                            size=size,
                            sha256=digest.hexdigest(),
                            mode=self._file_mode,
                        ),
                        source_path=child_path,
                        device=opened_stat.st_dev,
                        inode=opened_stat.st_ino,
                        mtime_ns=opened_stat.st_mtime_ns,
                    )
                )
                selected_bytes += size

        walk(root_absolute, ())
        selected.sort(
            key=lambda item: (
                check_deadline(),
                item.entry.path,
            )[1]
        )
        if not selected:
            includes = ", ".join(matcher.source for matcher in self._includes)
            raise PackagingError(f"include patterns selected no files: {includes}")
        return tuple(selected)

    def _is_selected(self, relative_path: str) -> bool:
        included = any(matcher.matches(relative_path) for matcher in self._includes)
        excluded = any(matcher.matches(relative_path) for matcher in self._excludes)
        return included and not excluded


def build_deterministic_zip(
    source_root: Path,
    destination: Path,
    *,
    includes: Sequence[str],
    excludes: Sequence[str] = (),
    check_deadline: DeadlineCheck,
) -> DeterministicZipResult:
    return DeterministicZipBuilder(includes=includes, excludes=excludes).build(
        source_root,
        destination,
        check_deadline=check_deadline,
    )


def fingerprint_tree(
    source_root: Path,
    *,
    includes: Sequence[str],
    excludes: Sequence[str] = (),
    check_deadline: DeadlineCheck,
) -> TreeFingerprint:
    return DeterministicZipBuilder(includes=includes, excludes=excludes).fingerprint(
        source_root,
        check_deadline=check_deadline,
    )


def verify_deterministic_zip(
    zip_path: Path,
    *,
    check_deadline: DeadlineCheck,
    timestamp: tuple[int, int, int, int, int, int] = NORMALIZED_TIMESTAMP,
    file_mode: int = NORMALIZED_FILE_MODE,
    compresslevel: int = 9,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    max_member_count: int = DEFAULT_MAX_MEMBER_COUNT,
    max_member_uncompressed_bytes: int = DEFAULT_MAX_MEMBER_UNCOMPRESSED_BYTES,
    max_total_uncompressed_bytes: int = DEFAULT_MAX_TOTAL_UNCOMPRESSED_BYTES,
    max_expansion_ratio: float = DEFAULT_MAX_EXPANSION_RATIO,
    max_total_expansion_ratio: float = DEFAULT_MAX_TOTAL_EXPANSION_RATIO,
) -> DeterministicZipResult:
    deadline_guard = _DeadlineGuard(check_deadline)
    check_deadline = deadline_guard
    check_deadline()
    _validate_verifier_limits(
        compresslevel=compresslevel,
        max_archive_bytes=max_archive_bytes,
        max_member_count=max_member_count,
        max_member_uncompressed_bytes=max_member_uncompressed_bytes,
        max_total_uncompressed_bytes=max_total_uncompressed_bytes,
        max_expansion_ratio=max_expansion_ratio,
        max_total_expansion_ratio=max_total_expansion_ratio,
    )
    if timestamp < NORMALIZED_TIMESTAMP:
        raise ValueError("ZIP timestamps cannot predate 1980-01-01")
    if file_mode < 0 or file_mode > 0o777:
        raise ValueError("file_mode must be a POSIX permission value")
    zip_path = Path(zip_path)
    check_deadline()
    source_stat = zip_path.lstat()
    check_deadline()
    if zip_path.is_symlink():
        raise UnsafeArchiveError(f"ZIP path is a symlink: {zip_path}")
    if not stat.S_ISREG(source_stat.st_mode):
        raise UnsafeArchiveError(f"ZIP path is not a regular file: {zip_path}")
    if source_stat.st_size > max_archive_bytes:
        raise UnsafeArchiveError(
            "ZIP exceeds the configured archive byte limit"
        )

    entries: list[TreeEntry] = []
    seen_casefolded: dict[str, str] = {}
    zip_sha256 = ""
    size_bytes = 0
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        check_deadline()
        source_descriptor = os.open(zip_path, flags)
        with os.fdopen(source_descriptor, "rb") as source_stream:
            opened_stat = os.fstat(source_stream.fileno())
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or (
                    source_stat.st_dev != 0
                    and source_stat.st_ino != 0
                    and not os.path.samestat(
                        source_stat,
                        opened_stat,
                    )
                )
            ):
                    raise UnsafeArchiveError(
                        f"ZIP path is not a regular file: {zip_path}"
                    )
            if opened_stat.st_size > max_archive_bytes:
                    raise UnsafeArchiveError(
                        "ZIP exceeds the configured archive byte limit"
                    )
            source = _CheckedBinaryReader(
                    source_stream,
                    check_deadline=check_deadline,
                    maximum_bytes=max_archive_bytes,
            )
            _preflight_zip_directory(
                    source,
                    archive_size=opened_stat.st_size,
                    max_member_count=max_member_count,
                    check_deadline=check_deadline,
            )
            check_deadline()
            comparison_descriptor = os.open(zip_path, flags)
            with os.fdopen(comparison_descriptor, "rb") as comparison_stream:
                    comparison_stat = os.fstat(comparison_stream.fileno())
                    if (
                        (comparison_stat.st_dev, comparison_stat.st_ino)
                        != (opened_stat.st_dev, opened_stat.st_ino)
                        or comparison_stat.st_size != opened_stat.st_size
                        or comparison_stat.st_mtime_ns
                        != opened_stat.st_mtime_ns
                    ):
                        raise UnsafeArchiveError(
                            "ZIP changed before canonical verification"
                        )
                    comparison_source = _CheckedBinaryReader(
                        comparison_stream,
                        check_deadline=check_deadline,
                        maximum_bytes=max_archive_bytes,
                    )
                    canonical_output = _ComparingBinaryWriter(
                        comparison_source,
                        archive_size=opened_stat.st_size,
                        check_deadline=check_deadline,
                    )
                    source.seek(0)
                    with (
                        zipfile.ZipFile(source, mode="r") as archive,
                        zipfile.ZipFile(
                            canonical_output,
                            mode="w",
                            compression=zipfile.ZIP_DEFLATED,
                            compresslevel=compresslevel,
                            strict_timestamps=True,
                        ) as canonical_archive,
                        _AbortComparisonOnError(canonical_output),
                    ):
                        canonical_archive.filelist = _DeadlineCheckedList(
                            canonical_archive.filelist,
                            check_deadline,
                    )
                        if archive.comment:
                            raise UnsafeArchiveError(
                                "deterministic ZIPs must not contain an archive comment"
                            )
                        infos = archive.infolist()
                        if len(infos) > max_member_count:
                            raise UnsafeArchiveError(
                                "ZIP exceeds the configured member count limit"
                            )
                        names: list[str] = []
                        for info in infos:
                            check_deadline()
                            names.append(info.filename)
                        if names != sorted(
                            names,
                            key=lambda name: (
                                check_deadline(),
                                name,
                            )[1],
                        ):
                            raise UnsafeArchiveError(
                                "ZIP members are not in stable path order"
                            )

                        declared_total = 0
                        compressed_total = 0
                        for info in infos:
                            check_deadline()
                            _validate_member_path(
                                info.filename,
                                error_type=UnsafeArchiveError,
                            )
                            if info.is_dir():
                                raise UnsafeArchiveError(
                                    "deterministic ZIPs must not contain directory "
                                    f"entries: {info.filename}"
                                )
                            unix_mode = info.external_attr >> 16
                            if stat.S_ISLNK(unix_mode):
                                raise UnsafeArchiveError(
                                    f"ZIP symlinks are not permitted: {info.filename}"
                                )
                            if info.create_system != 3 or not stat.S_ISREG(unix_mode):
                                raise UnsafeArchiveError(
                                    "ZIP member is not a normalized POSIX file: "
                                    f"{info.filename}"
                                )
                            if unix_mode & 0o777 != file_mode:
                                raise UnsafeArchiveError(
                                    "ZIP member has non-normalized permissions: "
                                    f"{info.filename}"
                                )
                            if info.date_time != timestamp:
                                raise UnsafeArchiveError(
                                    "ZIP member has non-normalized timestamp: "
                                    f"{info.filename}"
                                )
                            if info.compress_type != zipfile.ZIP_DEFLATED:
                                raise UnsafeArchiveError(
                                    "ZIP member has unexpected compression: "
                                    f"{info.filename}"
                                )
                            if info.flag_bits & 0x1:
                                raise UnsafeArchiveError(
                                    "encrypted ZIP members are not permitted: "
                                    f"{info.filename}"
                                )
                            if info.extra or info.comment:
                                raise UnsafeArchiveError(
                                    "ZIP member contains non-deterministic metadata: "
                                    f"{info.filename}"
                                )

                            casefolded = info.filename.casefold()
                            previous = seen_casefolded.get(casefolded)
                            if previous is not None:
                                raise UnsafeArchiveError(
                                    "case-insensitive archive path collision: "
                                    f"{previous}, {info.filename}"
                                )
                            seen_casefolded[casefolded] = info.filename

                            if info.file_size > max_member_uncompressed_bytes:
                                raise UnsafeArchiveError(
                                    "ZIP member exceeds the configured per-member "
                                    f"byte limit: {info.filename}"
                                )
                            declared_total += info.file_size
                            if declared_total > max_total_uncompressed_bytes:
                                raise UnsafeArchiveError(
                                    "ZIP exceeds the configured total uncompressed "
                                    "byte limit"
                                )
                            ratio = _expansion_ratio(
                                info.file_size,
                                info.compress_size,
                            )
                            if ratio > max_expansion_ratio:
                                raise UnsafeArchiveError(
                                    "ZIP member exceeds the configured expansion "
                                    f"ratio limit: {info.filename}"
                                )
                            compressed_total += info.compress_size

                        if (
                            _expansion_ratio(
                                declared_total,
                                compressed_total,
                            )
                            > max_total_expansion_ratio
                        ):
                            raise UnsafeArchiveError(
                                "ZIP exceeds the configured total expansion ratio limit"
                            )

                        for info in infos:
                            check_deadline()
                            digest = hashlib.sha256()
                            actual_size = 0
                            canonical_info = _canonical_zip_info(
                                info.filename,
                                timestamp=timestamp,
                                file_mode=file_mode,
                            )
                            canonical_info.file_size = info.file_size
                            with (
                                archive.open(info, mode="r") as member_source,
                                canonical_archive.open(
                                    canonical_info,
                                    mode="w",
                                ) as member_destination,
                            ):
                                while True:
                                    check_deadline()
                                    chunk = member_source.read(
                                        _READ_CHUNK_BYTES
                                    )
                                    check_deadline()
                                    if not chunk:
                                        break
                                    actual_size += len(chunk)
                                    if (
                                        actual_size > info.file_size
                                        or actual_size
                                        > max_member_uncompressed_bytes
                                    ):
                                        raise UnsafeArchiveError(
                                            "ZIP member expanded beyond its declared "
                                            "or configured byte limit: "
                                            f"{info.filename}"
                                        )
                                    digest.update(chunk)
                                    check_deadline()
                                    member_destination.write(chunk)
                                    check_deadline()
                            if actual_size != info.file_size:
                                raise UnsafeArchiveError(
                                    "ZIP member size does not match its header: "
                                    f"{info.filename}"
                                )
                            entries.append(
                                TreeEntry(
                                    path=info.filename,
                                    size=actual_size,
                                    sha256=digest.hexdigest(),
                                    mode=file_mode,
                                )
                            )
                    canonical_output.verify_complete()
                    zip_sha256, size_bytes = _hash_checked_archive(
                        source,
                        check_deadline=check_deadline,
                        maximum_bytes=max_archive_bytes,
                    )
            closed_stat = os.fstat(source_stream.fileno())
            if (
                    (closed_stat.st_dev, closed_stat.st_ino)
                    != (opened_stat.st_dev, opened_stat.st_ino)
                    or closed_stat.st_size != opened_stat.st_size
                    or closed_stat.st_mtime_ns != opened_stat.st_mtime_ns
            ):
                    raise UnsafeArchiveError(
                        "ZIP changed while it was being verified"
                    )
    except UnsafeArchiveError:
        raise
    except (
        zipfile.BadZipFile,
        zlib.error,
        EOFError,
        OSError,
        ValueError,
    ) as exc:
        if deadline_guard.failure is not None:
            deadline_guard.raise_if_failed()
        raise UnsafeArchiveError(f"invalid ZIP archive: {zip_path}") from exc

    if not entries:
        raise UnsafeArchiveError("deterministic ZIP must contain at least one file")
    normalized_entries = tuple(entries)
    result = DeterministicZipResult(
        zip_path=zip_path,
        entries=normalized_entries,
        tree_sha256=_hash_tree(
            normalized_entries,
            check_deadline=check_deadline,
        ),
        zip_sha256=zip_sha256,
        size_bytes=size_bytes,
    )
    deadline_guard.raise_if_failed()
    return result


def _preflight_zip_directory(
    archive: _CheckedBinaryReader,
    *,
    archive_size: int,
    max_member_count: int,
    check_deadline: DeadlineCheck,
) -> None:
    check_deadline()
    eocd_offset = _find_eocd_offset(
        archive,
        archive_size=archive_size,
        check_deadline=check_deadline,
    )
    eocd = _read_archive_at(
        archive,
        offset=eocd_offset,
        size=_EOCD_SIZE,
        archive_size=archive_size,
        check_deadline=check_deadline,
    )
    (
        _,
        disk_number,
        central_directory_disk,
        entries_on_disk,
        member_count,
        central_directory_size,
        central_directory_offset,
        comment_length,
    ) = struct.unpack("<4sHHHHIIH", eocd)
    if comment_length:
        raise UnsafeArchiveError(
            "deterministic ZIPs must not contain an archive comment"
        )

    classic_requires_zip64 = (
        entries_on_disk == 0xFFFF
        or member_count == 0xFFFF
        or central_directory_size == 0xFFFFFFFF
        or central_directory_offset == 0xFFFFFFFF
    )
    locator_offset = eocd_offset - _ZIP64_LOCATOR_SIZE
    has_physical_zip64_locator = (
        locator_offset >= 0
        and _read_archive_at(
            archive,
            offset=locator_offset,
            size=4,
            archive_size=archive_size,
            check_deadline=check_deadline,
        )
        == _ZIP64_LOCATOR_SIGNATURE
    )
    directory_boundary = eocd_offset
    if has_physical_zip64_locator or classic_requires_zip64:
        classic_entries_on_disk = entries_on_disk
        classic_member_count = member_count
        classic_directory_size = central_directory_size
        classic_directory_offset = central_directory_offset
        (
            member_count,
            central_directory_size,
            central_directory_offset,
            directory_boundary,
        ) = _parse_zip64_directory(
            archive,
            archive_size=archive_size,
            eocd_offset=eocd_offset,
            check_deadline=check_deadline,
        )
        if not classic_requires_zip64:
            raise UnsafeArchiveError(
                "unexpected physical ZIP64 locator without classic ZIP64 sentinels"
            )
        if (
            disk_number not in {0, 0xFFFF}
            or central_directory_disk not in {0, 0xFFFF}
            or (
                classic_entries_on_disk != 0xFFFF
                and classic_entries_on_disk != member_count
            )
            or (
                classic_member_count != 0xFFFF
                and classic_member_count != member_count
            )
            or (
                classic_directory_size != 0xFFFFFFFF
                and classic_directory_size != central_directory_size
            )
            or (
                classic_directory_offset != 0xFFFFFFFF
                and classic_directory_offset != central_directory_offset
            )
        ):
            raise UnsafeArchiveError(
                "classic and ZIP64 central directory metadata are inconsistent"
            )
    elif (
        disk_number != 0
        or central_directory_disk != 0
        or entries_on_disk != member_count
    ):
        raise UnsafeArchiveError("multi-disk ZIP archives are not permitted")

    if member_count > max_member_count:
        raise UnsafeArchiveError(
            "ZIP exceeds the configured member count limit"
        )
    if (
        central_directory_offset > directory_boundary
        or central_directory_size
        > directory_boundary - central_directory_offset
    ):
        raise UnsafeArchiveError(
            "ZIP central directory bounds exceed the archive"
        )

    central_directory_end = (
        central_directory_offset + central_directory_size
    )
    # ZipFile treats this gap as a concatenation offset; require both parsers to agree.
    if central_directory_end != directory_boundary:
        raise UnsafeArchiveError(
            "ZIP central directory must be contiguous with its EOCD or ZIP64 boundary"
        )

    cursor = central_directory_offset
    expected_local_offset = 0
    for _ in range(member_count):
        check_deadline()
        if cursor + _CENTRAL_DIRECTORY_HEADER_SIZE > central_directory_end:
            raise UnsafeArchiveError(
                "ZIP central directory bounds do not match its member count"
            )
        central_header = _read_archive_at(
            archive,
            offset=cursor,
            size=_CENTRAL_DIRECTORY_HEADER_SIZE,
            archive_size=archive_size,
            check_deadline=check_deadline,
        )
        if central_header[:4] != _CENTRAL_DIRECTORY_SIGNATURE:
            raise UnsafeArchiveError(
                "ZIP central directory bounds do not match its member count"
            )
        filename_length, extra_length, member_comment_length = struct.unpack_from(
            "<HHH",
            central_header,
            28,
        )
        entry_size = (
            _CENTRAL_DIRECTORY_HEADER_SIZE
            + filename_length
            + extra_length
            + member_comment_length
        )
        if entry_size > central_directory_end - cursor:
            raise UnsafeArchiveError(
                "ZIP central directory bounds contain a truncated member"
            )

        flag_bits = struct.unpack_from("<H", central_header, 8)[0]
        compressed_size = struct.unpack_from("<I", central_header, 20)[0]
        local_offset = struct.unpack_from("<I", central_header, 42)[0]
        if compressed_size == 0xFFFFFFFF or local_offset == 0xFFFFFFFF:
            raise UnsafeArchiveError(
                "ZIP64 member offsets or sizes are not canonical for a bounded archive"
            )
        if local_offset != expected_local_offset:
            raise UnsafeArchiveError(
                "ZIP local file layout is prepended, gapped, or out of order"
            )
        if (
            flag_bits & 0x08
            or local_offset + _LOCAL_FILE_HEADER_SIZE
            > central_directory_offset
        ):
            raise UnsafeArchiveError(
                "ZIP local file layout is ambiguous or uses a data descriptor"
            )
        local_header = _read_archive_at(
            archive,
            offset=local_offset,
            size=_LOCAL_FILE_HEADER_SIZE,
            archive_size=archive_size,
            check_deadline=check_deadline,
        )
        if local_header[:4] != _LOCAL_FILE_HEADER_SIGNATURE:
            raise UnsafeArchiveError(
                "ZIP local file layout is ambiguous or uses a data descriptor"
            )
        local_filename_length, local_extra_length = struct.unpack_from(
            "<HH",
            local_header,
            26,
        )
        local_name_start = local_offset + _LOCAL_FILE_HEADER_SIZE
        local_data_start = (
            local_name_start + local_filename_length + local_extra_length
        )
        local_entry_end = local_data_start + compressed_size
        if local_entry_end > central_directory_offset:
            raise UnsafeArchiveError(
                "ZIP local file layout overlaps the central directory"
            )
        central_name_start = cursor + _CENTRAL_DIRECTORY_HEADER_SIZE
        if (
            _read_archive_at(
                archive,
                offset=local_name_start,
                size=local_filename_length,
                archive_size=archive_size,
                check_deadline=check_deadline,
            )
            != _read_archive_at(
                archive,
                offset=central_name_start,
                size=filename_length,
                archive_size=archive_size,
                check_deadline=check_deadline,
            )
        ):
            raise UnsafeArchiveError(
                "ZIP local and central directory member names do not match"
            )
        expected_local_offset = local_entry_end
        cursor += entry_size
    if cursor != central_directory_end:
        raise UnsafeArchiveError(
            "ZIP central directory bounds contain undeclared members or data"
        )
    if expected_local_offset != central_directory_offset:
        raise UnsafeArchiveError(
            "ZIP local file layout is not contiguous with the central directory"
        )


def _find_eocd_offset(
    archive: _CheckedBinaryReader,
    *,
    archive_size: int,
    check_deadline: DeadlineCheck,
) -> int:
    check_deadline()
    search_start = max(0, archive_size - (_EOCD_SIZE + 0xFFFF))
    tail = _read_archive_at(
        archive,
        offset=search_start,
        size=archive_size - search_start,
        archive_size=archive_size,
        check_deadline=check_deadline,
    )
    candidate = tail.rfind(_EOCD_SIGNATURE)
    while candidate >= 0:
        check_deadline()
        if candidate + _EOCD_SIZE <= len(tail):
            comment_length = struct.unpack_from(
                "<H",
                tail,
                candidate + 20,
            )[0]
            if candidate + _EOCD_SIZE + comment_length == len(tail):
                return search_start + candidate
        candidate = tail.rfind(
            _EOCD_SIGNATURE,
            0,
            candidate,
        )
    raise UnsafeArchiveError(
        "ZIP end-of-central-directory record is missing or out of bounds"
    )


def _parse_zip64_directory(
    archive: _CheckedBinaryReader,
    *,
    archive_size: int,
    eocd_offset: int,
    check_deadline: DeadlineCheck,
) -> tuple[int, int, int, int]:
    check_deadline()
    locator_offset = eocd_offset - _ZIP64_LOCATOR_SIZE
    if (
        locator_offset < 0
    ):
        raise UnsafeArchiveError(
            "ZIP64 end-of-central-directory locator is missing or out of bounds"
        )
    locator = _read_archive_at(
        archive,
        offset=locator_offset,
        size=_ZIP64_LOCATOR_SIZE,
        archive_size=archive_size,
        check_deadline=check_deadline,
    )
    if locator[:4] != _ZIP64_LOCATOR_SIGNATURE:
        raise UnsafeArchiveError(
            "ZIP64 end-of-central-directory locator is missing or out of bounds"
        )
    (
        _,
        zip64_disk,
        zip64_eocd_offset,
        total_disks,
    ) = struct.unpack("<4sIQI", locator)
    if zip64_disk != 0 or total_disks != 1:
        raise UnsafeArchiveError("multi-disk ZIP64 archives are not permitted")
    physical_zip64_eocd_offset = locator_offset - _ZIP64_EOCD_MIN_SIZE
    if (
        physical_zip64_eocd_offset < 0
        or zip64_eocd_offset != physical_zip64_eocd_offset
    ):
        raise UnsafeArchiveError(
            "ZIP64 end-of-central-directory record is missing or out of bounds"
        )
    zip64_record = _read_archive_at(
        archive,
        offset=physical_zip64_eocd_offset,
        size=_ZIP64_EOCD_MIN_SIZE,
        archive_size=archive_size,
        check_deadline=check_deadline,
    )
    if zip64_record[:4] != _ZIP64_EOCD_SIGNATURE:
        raise UnsafeArchiveError(
            "ZIP64 end-of-central-directory record is missing or out of bounds"
        )
    (
        _,
        zip64_record_size,
        _,
        _,
        disk_number,
        central_directory_disk,
        entries_on_disk,
        member_count,
        central_directory_size,
        central_directory_offset,
    ) = struct.unpack_from(
        "<4sQHHIIQQQQ",
        zip64_record,
    )
    zip64_record_end = (
        physical_zip64_eocd_offset + 12 + zip64_record_size
    )
    if (
        zip64_record_size != 44
        or zip64_record_end != locator_offset
        or disk_number != 0
        or central_directory_disk != 0
        or entries_on_disk != member_count
    ):
        raise UnsafeArchiveError(
            "ZIP64 central directory metadata is inconsistent or out of bounds"
        )
    return (
        member_count,
        central_directory_size,
        central_directory_offset,
        physical_zip64_eocd_offset,
    )


def _read_archive_at(
    archive: _CheckedBinaryReader,
    *,
    offset: int,
    size: int,
    archive_size: int,
    check_deadline: DeadlineCheck,
) -> bytes:
    check_deadline()
    if (
        offset < 0
        or size < 0
        or offset > archive_size
        or size > archive_size - offset
    ):
        raise UnsafeArchiveError("ZIP archive bounds are invalid")
    archive.seek(offset)
    payload = archive.read(size)
    check_deadline()
    if len(payload) != size:
        raise UnsafeArchiveError("ZIP archive is truncated")
    return payload


def _hash_checked_archive(
    source: _CheckedBinaryReader,
    *,
    check_deadline: DeadlineCheck,
    maximum_bytes: int,
) -> tuple[str, int]:
    check_deadline()
    source.seek(0)
    digest = hashlib.sha256()
    total = 0
    while True:
        check_deadline()
        source_chunk = source.read(_READ_CHUNK_BYTES)
        check_deadline()
        if not source_chunk:
            break
        total += len(source_chunk)
        if total > maximum_bytes:
            raise UnsafeArchiveError(
                "ZIP exceeds the configured archive byte limit"
            )
        digest.update(source_chunk)
    check_deadline()
    return digest.hexdigest(), total


def _serialize_selected(
    selected: Sequence[_SelectedFile],
    *,
    destination: Path,
    timestamp: tuple[int, int, int, int, int, int],
    file_mode: int,
    compresslevel: int,
    check_deadline: DeadlineCheck,
    max_archive_bytes: int,
) -> None:
    check_deadline()
    flags = os.O_RDWR | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    output_descriptor = os.open(destination, flags, 0o600)
    with os.fdopen(output_descriptor, "w+b") as output_stream:
        checked_output = _CheckedBinaryWriter(
            output_stream,
            check_deadline=check_deadline,
            maximum_bytes=max_archive_bytes,
        )
        with zipfile.ZipFile(
            checked_output,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=compresslevel,
            strict_timestamps=True,
        ) as archive:
            archive.filelist = _DeadlineCheckedList(
                archive.filelist,
                check_deadline,
            )
            for item in selected:
                check_deadline()
                info = _canonical_zip_info(
                    item.entry.path,
                    timestamp=timestamp,
                    file_mode=file_mode,
                )
                info.file_size = item.entry.size
                source_flags = os.O_RDONLY
                if hasattr(os, "O_BINARY"):
                    source_flags |= os.O_BINARY
                if hasattr(os, "O_NOFOLLOW"):
                    source_flags |= os.O_NOFOLLOW
                check_deadline()
                descriptor = os.open(item.source_path, source_flags)
                with (
                    os.fdopen(descriptor, "rb") as source,
                    archive.open(info, mode="w") as compressed,
                ):
                    opened = os.fstat(source.fileno())
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or (opened.st_dev, opened.st_ino)
                        != (item.device, item.inode)
                        or opened.st_size != item.entry.size
                        or opened.st_mtime_ns != item.mtime_ns
                    ):
                        raise UnsafeSourcePathError(
                            "source changed before compression: "
                            f"{item.entry.path}"
                        )
                    digest = hashlib.sha256()
                    size = 0
                    while True:
                        check_deadline()
                        chunk = source.read(_READ_CHUNK_BYTES)
                        check_deadline()
                        if not chunk:
                            break
                        size += len(chunk)
                        digest.update(chunk)
                        check_deadline()
                        compressed.write(chunk)
                        check_deadline()
                    closed = os.fstat(source.fileno())
                    if (
                        (closed.st_dev, closed.st_ino)
                        != (item.device, item.inode)
                        or closed.st_size != size
                        or closed.st_mtime_ns != item.mtime_ns
                        or size != item.entry.size
                        or digest.hexdigest() != item.entry.sha256
                    ):
                        raise UnsafeSourcePathError(
                            "source changed during compression: "
                            f"{item.entry.path}"
                        )
            check_deadline()
        checked_output.flush()
        os.fsync(output_stream.fileno())
    check_deadline()


def _serialize_verified_entries(
    entries: Sequence[TreeEntry],
    *,
    destination: Path,
    open_source: VerifiedSourceOpener,
    timestamp: tuple[int, int, int, int, int, int],
    file_mode: int,
    compresslevel: int,
    check_deadline: DeadlineCheck,
    max_archive_bytes: int,
) -> None:
    check_deadline()
    flags = os.O_RDWR | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    output_descriptor = os.open(destination, flags, 0o600)
    with os.fdopen(output_descriptor, "w+b") as output_stream:
        checked_output = _CheckedBinaryWriter(
            output_stream,
            check_deadline=check_deadline,
            maximum_bytes=max_archive_bytes,
        )
        with zipfile.ZipFile(
            checked_output,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=compresslevel,
            strict_timestamps=True,
        ) as archive:
            archive.filelist = _DeadlineCheckedList(
                archive.filelist,
                check_deadline,
            )
            for entry in entries:
                check_deadline()
                info = _canonical_zip_info(
                    entry.path,
                    timestamp=timestamp,
                    file_mode=file_mode,
                )
                info.file_size = entry.size
                check_deadline()
                try:
                    source_context = open_source(entry.path)
                    with (
                        source_context as source,
                        archive.open(info, mode="w") as compressed,
                    ):
                        digest = hashlib.sha256()
                        size = 0
                        while True:
                            check_deadline()
                            chunk = source.read(_READ_CHUNK_BYTES)
                            check_deadline()
                            if not chunk:
                                break
                            if not isinstance(chunk, bytes):
                                raise PackagingError(
                                    "verified source stream returned non-bytes: "
                                    f"{entry.path}"
                                )
                            size += len(chunk)
                            if size > entry.size:
                                raise UnsafeSourcePathError(
                                    "verified source grew while packaging: "
                                    f"{entry.path}"
                                )
                            digest.update(chunk)
                            check_deadline()
                            compressed.write(chunk)
                            check_deadline()
                except (PackagingError, UnsafeSourcePathError):
                    raise
                except OSError as exc:
                    raise PackagingError(
                        f"cannot read verified source file: {entry.path}"
                    ) from exc
                if size != entry.size or digest.hexdigest() != entry.sha256:
                    raise UnsafeSourcePathError(
                        "verified source changed while packaging: "
                        f"{entry.path}"
                    )
            check_deadline()
        checked_output.flush()
        os.fsync(output_stream.fileno())
    check_deadline()


def _canonical_zip_info(
    path: str,
    *,
    timestamp: tuple[int, int, int, int, int, int],
    file_mode: int,
) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=timestamp)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | file_mode) << 16
    info.internal_attr = 0
    info.extra = b""
    info.comment = b""
    return info


def _validate_verifier_limits(
    *,
    compresslevel: int,
    max_archive_bytes: int,
    max_member_count: int,
    max_member_uncompressed_bytes: int,
    max_total_uncompressed_bytes: int,
    max_expansion_ratio: float,
    max_total_expansion_ratio: float,
) -> None:
    if compresslevel < 0 or compresslevel > 9:
        raise ValueError("compresslevel must be between 0 and 9")
    integer_limits = {
        "max_archive_bytes": max_archive_bytes,
        "max_member_count": max_member_count,
        "max_member_uncompressed_bytes": max_member_uncompressed_bytes,
        "max_total_uncompressed_bytes": max_total_uncompressed_bytes,
    }
    for name, value in integer_limits.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    ratio_limits = {
        "max_expansion_ratio": max_expansion_ratio,
        "max_total_expansion_ratio": max_total_expansion_ratio,
    }
    for name, value in ratio_limits.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{name} must be positive")


def _expansion_ratio(uncompressed_bytes: int, compressed_bytes: int) -> float:
    if uncompressed_bytes == 0:
        return 0.0
    if compressed_bytes <= 0:
        return float("inf")
    return uncompressed_bytes / compressed_bytes


def _hash_tree(
    entries: Iterable[TreeEntry],
    *,
    check_deadline: DeadlineCheck,
) -> str:
    check_deadline()
    digest = hashlib.sha256()
    digest.update(_TREE_DOMAIN)
    for entry in entries:
        check_deadline()
        path_bytes = entry.path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(4, "big"))
        digest.update(path_bytes)
        digest.update(entry.size.to_bytes(8, "big"))
        digest.update(entry.mode.to_bytes(4, "big"))
        digest.update(bytes.fromhex(entry.sha256))
    check_deadline()
    return digest.hexdigest()


def _hash_file(
    path: Path,
    *,
    check_deadline: DeadlineCheck,
    maximum_bytes: int,
) -> tuple[str, int]:
    check_deadline()
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise PackagingError(f"output is not a regular file: {path}")
            while True:
                check_deadline()
                chunk = source.read(_READ_CHUNK_BYTES)
                check_deadline()
                if not chunk:
                    break
                size += len(chunk)
                if size > maximum_bytes:
                    raise UnsafeArchiveError(
                        "ZIP exceeds the configured archive byte limit"
                    )
                digest.update(chunk)
            closed = os.fstat(source.fileno())
            if (
                (closed.st_dev, closed.st_ino)
                != (opened.st_dev, opened.st_ino)
                or closed.st_size != size
                or closed.st_mtime_ns != opened.st_mtime_ns
            ):
                raise PackagingError(f"output changed while hashing: {path}")
    except PackagingError:
        raise
    except OSError as exc:
        raise PackagingError(f"cannot hash output file: {path}") from exc
    check_deadline()
    return digest.hexdigest(), size


def _validate_output_path(destination: Path) -> None:
    if destination.exists() and destination.is_symlink():
        raise UnsafeSourcePathError(f"destination is a symlink: {destination}")
    if destination.exists() and not destination.is_file():
        raise PackagingError(f"destination is not a regular file: {destination}")


def _compile_pattern(value: str) -> _PathMatcher:
    normalized = _normalize_pattern(value)
    if not any(character in normalized for character in _GLOB_CHARS):
        expression = re.escape(normalized) + r"(?:/.*)?"
        return _PathMatcher(normalized, re.compile(expression))

    expression_parts: list[str] = []
    index = 0
    while index < len(normalized):
        character = normalized[index]
        if character == "*":
            if index + 1 < len(normalized) and normalized[index + 1] == "*":
                index += 2
                if index < len(normalized) and normalized[index] == "/":
                    expression_parts.append(r"(?:.*/)?")
                    index += 1
                else:
                    expression_parts.append(r".*")
                continue
            expression_parts.append(r"[^/]*")
        elif character == "?":
            expression_parts.append(r"[^/]")
        else:
            expression_parts.append(re.escape(character))
        index += 1
    return _PathMatcher(normalized, re.compile("".join(expression_parts)))


def _normalize_pattern(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise UnsafeSourcePathError("include and exclude patterns must be non-empty strings")
    if "\x00" in value or "\\" in value or ":" in value:
        raise UnsafeSourcePathError(f"unsafe source pattern: {value}")
    while value.startswith("./"):
        value = value[2:]
    value = value.rstrip("/")
    _validate_member_path(value, error_type=UnsafeSourcePathError)
    return value


def _validate_member_path(
    value: str,
    *,
    error_type: type[PackagingError],
) -> None:
    if (
        not value
        or value == "."
        or "\x00" in value
        or "\\" in value
        or ":" in value
        or "//" in value
    ):
        raise error_type(f"unsafe archive path: {value!r}")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise error_type(f"absolute archive paths are not permitted: {value}")
    if ".." in posix.parts or ".." in windows.parts:
        raise error_type(f"archive path traversal is not permitted: {value}")
    if any(part in ("", ".") for part in posix.parts):
        raise error_type(f"invalid archive path: {value}")
