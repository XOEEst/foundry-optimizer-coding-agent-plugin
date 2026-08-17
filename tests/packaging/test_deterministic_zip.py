from __future__ import annotations

import os
import stat
import struct
import zipfile
from pathlib import Path

import pytest

from foundry_opt.packaging import (
    DeterministicZipBuilder as _DeterministicZipBuilder,
    UnsafeArchiveError,
    UnsafeSourcePathError,
    verify_deterministic_zip as _verify_deterministic_zip,
)


class _DeadlineExpired(RuntimeError):
    pass


def _no_deadline() -> None:
    pass


class DeterministicZipBuilder(_DeterministicZipBuilder):
    def build(
        self,
        source_root: Path,
        destination: Path,
        *,
        check_deadline=_no_deadline,
    ):
        return super().build(
            source_root,
            destination,
            check_deadline=check_deadline,
        )


def verify_deterministic_zip(
    zip_path: Path,
    *,
    check_deadline=_no_deadline,
    **kwargs: object,
):
    return _verify_deterministic_zip(
        zip_path,
        check_deadline=check_deadline,
        **kwargs,
    )


def test_packaging_operations_require_explicit_deadline_checks(
    tmp_path: Path,
) -> None:
    builder = _DeterministicZipBuilder(includes=("**/*",))
    with pytest.raises(TypeError, match="check_deadline"):
        builder.build(tmp_path, tmp_path / "output.zip")  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="check_deadline"):
        builder.fingerprint(tmp_path)  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="check_deadline"):
        _verify_deterministic_zip(tmp_path / "output.zip")  # type: ignore[call-arg]


def test_builder_checks_deadline_during_large_tree_walk(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for index in range(100):
        (source / f"{index:03}.txt").write_text("payload\n", encoding="utf-8")
    checks = 0

    def check_deadline() -> None:
        nonlocal checks
        checks += 1
        if checks == 25:
            raise _DeadlineExpired

    with pytest.raises(_DeadlineExpired):
        DeterministicZipBuilder(includes=("**/*",)).build(
            source,
            tmp_path / "candidate.zip",
            check_deadline=check_deadline,
        )

    assert checks == 25


def test_builder_stops_directory_enumeration_when_deadline_expires(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for index in range(100):
        (source / f"{index:03}.txt").write_text("payload\n", encoding="utf-8")
    original_scandir = os.scandir
    enumerated = 0

    class CountingScandir:
        def __init__(self, path: os.PathLike[str] | str) -> None:
            self._entries = original_scandir(path)

        def __enter__(self) -> CountingScandir:
            return self

        def __exit__(self, *args: object) -> None:
            self._entries.close()

        def __iter__(self) -> CountingScandir:
            return self

        def __next__(self) -> os.DirEntry[str]:
            nonlocal enumerated
            entry = next(self._entries)
            enumerated += 1
            return entry

    monkeypatch.setattr(os, "scandir", CountingScandir)

    def check_deadline() -> None:
        if enumerated >= 5:
            raise _DeadlineExpired

    with pytest.raises(_DeadlineExpired):
        DeterministicZipBuilder(includes=("**/*",)).build(
            source,
            tmp_path / "candidate.zip",
            check_deadline=check_deadline,
        )

    assert enumerated == 5


def test_builder_bounds_directory_entries_before_selection(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "selected.txt").write_text("selected\n", encoding="utf-8")
    (source / "ignored-one.bin").write_bytes(b"ignored")
    (source / "ignored-two.bin").write_bytes(b"ignored")

    with pytest.raises(UnsafeArchiveError, match="member count"):
        DeterministicZipBuilder(
            includes=("selected.txt",),
            max_member_count=2,
        ).build(
            source,
            tmp_path / "candidate.zip",
            check_deadline=_no_deadline,
        )


def test_builder_checks_deadline_during_source_stream_read(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "large.bin").write_bytes(b"x" * (5 * 1024 * 1024))
    checks = 0

    def check_deadline() -> None:
        nonlocal checks
        checks += 1
        if checks == 8:
            raise _DeadlineExpired

    with pytest.raises(_DeadlineExpired):
        DeterministicZipBuilder(includes=("**/*",)).build(
            source,
            tmp_path / "candidate.zip",
            check_deadline=check_deadline,
        )

    assert checks == 8


def test_verifier_checks_deadline_during_member_stream_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "large.bin").write_bytes(
        bytes(range(256)) * (20 * 1024)
    )
    result = DeterministicZipBuilder(includes=("**/*",)).build(
        source,
        tmp_path / "candidate.zip",
        check_deadline=_no_deadline,
    )
    stream_started = False
    stream_checks = 0
    original_read = zipfile.ZipExtFile.read

    def tracking_read(
        stream: zipfile.ZipExtFile,
        *args: object,
        **kwargs: object,
    ) -> bytes:
        nonlocal stream_started
        stream_started = True
        return original_read(stream, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipExtFile, "read", tracking_read)

    def check_deadline() -> None:
        nonlocal stream_checks
        if stream_started:
            stream_checks += 1
        if stream_checks == 3:
            raise _DeadlineExpired

    with pytest.raises(_DeadlineExpired):
        verify_deterministic_zip(
            result.zip_path,
            check_deadline=check_deadline,
        )

    assert stream_started
    assert stream_checks >= 3


def test_zip_is_deterministic_with_normalized_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "z.txt").write_text("z\n", encoding="utf-8")
    (source / "a.txt").write_text("a\n", encoding="utf-8")
    ignored = source / "ignored"
    ignored.mkdir()
    (ignored / "secret.txt").write_text("secret\n", encoding="utf-8")

    builder = DeterministicZipBuilder(
        includes=("**/*.txt",),
        excludes=("ignored/**",),
    )
    first = builder.build(source, tmp_path / "first.zip")
    os.utime(source / "a.txt", (2_000_000_000, 2_000_000_000))
    os.chmod(source / "a.txt", stat.S_IREAD | stat.S_IWRITE)
    second = builder.build(source, tmp_path / "second.zip")

    assert first.tree_sha256 == second.tree_sha256
    assert first.zip_sha256 == second.zip_sha256
    assert first.files == ("a.txt", "z.txt")
    assert first.zip_path.read_bytes() == second.zip_path.read_bytes()

    with zipfile.ZipFile(first.zip_path) as archive:
        infos = archive.infolist()
        assert [info.filename for info in infos] == ["a.txt", "z.txt"]
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in infos)
        assert all(info.create_system == 3 for info in infos)
        assert all((info.external_attr >> 16) & 0o777 == 0o644 for info in infos)

    verified = verify_deterministic_zip(first.zip_path)
    assert verified.tree_sha256 == first.tree_sha256
    assert verified.zip_sha256 == first.zip_sha256


@pytest.mark.parametrize(
    "pattern",
    ("../secret", "..\\secret", "C:\\Windows\\system32", "/absolute"),
)
def test_builder_rejects_path_traversal_patterns(pattern: str) -> None:
    with pytest.raises(UnsafeSourcePathError):
        DeterministicZipBuilder(includes=(pattern,))


def test_verifier_rejects_archive_path_traversal(tmp_path: Path) -> None:
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../escape.txt", b"escape")

    with pytest.raises(UnsafeArchiveError):
        verify_deterministic_zip(archive_path)


def test_builder_rejects_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = source / "target.txt"
    target.write_text("target", encoding="utf-8")
    link = source / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")

    with pytest.raises(UnsafeSourcePathError):
        DeterministicZipBuilder(includes=("**/*",)).build(
            source,
            tmp_path / "output.zip",
        )


def test_verifier_rejects_archive_comment(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print('safe')\n", encoding="utf-8")
    result = DeterministicZipBuilder(includes=("**/*",)).build(
        source,
        tmp_path / "commented.zip",
    )
    with zipfile.ZipFile(result.zip_path, "a") as archive:
        archive.comment = b"noncanonical"

    with pytest.raises(UnsafeArchiveError, match="comment"):
        verify_deterministic_zip(result.zip_path)


def test_verifier_rejects_noncanonical_zip_serialization(tmp_path: Path) -> None:
    archive_path = tmp_path / "noncanonical.zip"
    content = b"repeated-content\n" * 10_000
    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=1,
    ) as archive:
        info = zipfile.ZipInfo("main.py", date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | 0o644) << 16
        info.internal_attr = 0
        info.extra = b""
        info.comment = b""
        archive.writestr(
            info,
            content,
            compress_type=zipfile.ZIP_DEFLATED,
            compresslevel=1,
        )

    with pytest.raises(UnsafeArchiveError, match="canonical"):
        verify_deterministic_zip(archive_path)


@pytest.mark.parametrize(
    ("limit", "expected"),
    (
        ({"max_member_count": 1}, "member count"),
        ({"max_member_uncompressed_bytes": 4095}, "per-member"),
        ({"max_total_uncompressed_bytes": 8191}, "total uncompressed"),
        ({"max_expansion_ratio": 2.0}, "expansion ratio"),
        (
            {
                "max_expansion_ratio": 1000.0,
                "max_total_expansion_ratio": 2.0,
            },
            "total expansion ratio",
        ),
    ),
)
def test_verifier_enforces_archive_resource_limits(
    tmp_path: Path,
    limit: dict[str, int | float],
    expected: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.bin").write_bytes(b"\0" * 4096)
    (source / "b.bin").write_bytes(b"\0" * 4096)
    result = DeterministicZipBuilder(includes=("**/*",)).build(
        source,
        tmp_path / "limited.zip",
    )

    with pytest.raises(UnsafeArchiveError, match=expected):
        verify_deterministic_zip(result.zip_path, **limit)  # type: ignore[arg-type]


def test_verifier_enforces_archive_byte_limit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print('safe')\n", encoding="utf-8")
    result = DeterministicZipBuilder(includes=("**/*",)).build(
        source,
        tmp_path / "limited.zip",
    )

    with pytest.raises(UnsafeArchiveError, match="archive byte"):
        verify_deterministic_zip(
            result.zip_path,
            max_archive_bytes=result.size_bytes - 1,
        )


def test_eocd_member_count_is_rejected_before_zipfile_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("a", encoding="utf-8")
    (source / "b.txt").write_text("b", encoding="utf-8")
    result = DeterministicZipBuilder(includes=("**/*",)).build(
        source,
        tmp_path / "members.zip",
    )

    def forbidden_zipfile(*args: object, **kwargs: object) -> object:
        raise AssertionError("ZipFile constructed before member count preflight")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden_zipfile)
    with pytest.raises(UnsafeArchiveError, match="member count"):
        verify_deterministic_zip(result.zip_path, max_member_count=1)


def test_zip64_member_count_is_rejected_before_zipfile_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    zip64_record = struct.pack(
        "<4sQHHIIQQQQ",
        b"PK\x06\x06",
        44,
        45,
        45,
        0,
        0,
        2,
        2,
        0,
        0,
    )
    zip64_locator = struct.pack(
        "<4sIQI",
        b"PK\x06\x07",
        0,
        0,
        1,
    )
    eocd = struct.pack(
        "<4sHHHHIIH",
        b"PK\x05\x06",
        0,
        0,
        0xFFFF,
        0xFFFF,
        0xFFFFFFFF,
        0xFFFFFFFF,
        0,
    )
    archive_path = tmp_path / "zip64-count.zip"
    archive_path.write_bytes(zip64_record + zip64_locator + eocd)

    def forbidden_zipfile(*args: object, **kwargs: object) -> object:
        raise AssertionError("ZipFile constructed before ZIP64 count preflight")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden_zipfile)
    with pytest.raises(UnsafeArchiveError, match="member count"):
        verify_deterministic_zip(archive_path, max_member_count=1)


def test_central_directory_bounds_are_rejected_before_zipfile_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print('safe')\n", encoding="utf-8")
    result = DeterministicZipBuilder(includes=("**/*",)).build(
        source,
        tmp_path / "bounds.zip",
    )
    archive_bytes = bytearray(result.zip_path.read_bytes())
    eocd_offset = len(archive_bytes) - 22
    struct.pack_into("<I", archive_bytes, eocd_offset + 12, len(archive_bytes))
    result.zip_path.write_bytes(archive_bytes)

    def forbidden_zipfile(*args: object, **kwargs: object) -> object:
        raise AssertionError("ZipFile constructed before directory bounds preflight")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden_zipfile)
    with pytest.raises(UnsafeArchiveError, match="central directory bounds"):
        verify_deterministic_zip(result.zip_path)


def test_central_directory_cannot_hide_members_from_eocd_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("a", encoding="utf-8")
    (source / "b.txt").write_text("b", encoding="utf-8")
    result = DeterministicZipBuilder(includes=("**/*",)).build(
        source,
        tmp_path / "hidden-member.zip",
    )
    archive_bytes = bytearray(result.zip_path.read_bytes())
    eocd_offset = len(archive_bytes) - 22
    struct.pack_into("<HH", archive_bytes, eocd_offset + 8, 1, 1)
    result.zip_path.write_bytes(archive_bytes)

    def forbidden_zipfile(*args: object, **kwargs: object) -> object:
        raise AssertionError("ZipFile constructed before directory count scan")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden_zipfile)
    with pytest.raises(UnsafeArchiveError, match="central directory bounds"):
        verify_deterministic_zip(result.zip_path, max_member_count=1)


def test_central_directory_must_be_contiguous_with_eocd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print('safe')\n", encoding="utf-8")
    result = DeterministicZipBuilder(includes=("**/*",)).build(
        source,
        tmp_path / "gapped.zip",
    )
    archive_bytes = bytearray(result.zip_path.read_bytes())
    eocd_offset = len(archive_bytes) - 22
    archive_bytes[eocd_offset:eocd_offset] = b"GAP!"
    result.zip_path.write_bytes(archive_bytes)

    def forbidden_zipfile(*args: object, **kwargs: object) -> object:
        raise AssertionError("ZipFile constructed for gapped central directory")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden_zipfile)
    with pytest.raises(UnsafeArchiveError, match="contiguous"):
        verify_deterministic_zip(result.zip_path)


def test_adjusted_prepended_zip_is_rejected_before_zipfile_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print('safe')\n", encoding="utf-8")
    result = DeterministicZipBuilder(includes=("**/*",)).build(
        source,
        tmp_path / "prepended.zip",
    )
    original = result.zip_path.read_bytes()
    original_eocd = len(original) - 22
    original_central_offset = struct.unpack_from(
        "<I",
        original,
        original_eocd + 16,
    )[0]
    prefix = b"JUNK"
    archive_bytes = bytearray(prefix + original)
    central_offset = original_central_offset + len(prefix)
    eocd_offset = original_eocd + len(prefix)
    struct.pack_into("<I", archive_bytes, eocd_offset + 16, central_offset)
    original_local_offset = struct.unpack_from(
        "<I",
        archive_bytes,
        central_offset + 42,
    )[0]
    struct.pack_into(
        "<I",
        archive_bytes,
        central_offset + 42,
        original_local_offset + len(prefix),
    )
    result.zip_path.write_bytes(archive_bytes)

    def forbidden_zipfile(*args: object, **kwargs: object) -> object:
        raise AssertionError("ZipFile constructed for prepended archive")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden_zipfile)
    with pytest.raises(UnsafeArchiveError, match="prepended|local file layout"):
        verify_deterministic_zip(result.zip_path)


def test_concatenated_zip_cannot_redirect_zipfile_to_another_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print('safe')\n", encoding="utf-8")
    builder = DeterministicZipBuilder(includes=("**/*",))
    first = builder.build(source, tmp_path / "first.zip").zip_path.read_bytes()
    second_result = builder.build(source, tmp_path / "second.zip")
    second = second_result.zip_path.read_bytes()
    second_result.zip_path.write_bytes(first + second)

    def forbidden_zipfile(*args: object, **kwargs: object) -> object:
        raise AssertionError("ZipFile constructed for concatenated archive")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden_zipfile)
    with pytest.raises(UnsafeArchiveError, match="contiguous"):
        verify_deterministic_zip(second_result.zip_path)


def test_physical_zip64_locator_signature_cannot_hide_in_central_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print('safe')\n", encoding="utf-8")
    result = DeterministicZipBuilder(includes=("**/*",)).build(
        source,
        tmp_path / "zip64-smuggled.zip",
    )
    archive_bytes = bytearray(result.zip_path.read_bytes())
    original_eocd = len(archive_bytes) - 22
    central_size = struct.unpack_from("<I", archive_bytes, original_eocd + 12)[0]
    central_offset = struct.unpack_from("<I", archive_bytes, original_eocd + 16)[0]
    locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, 0, 1)
    struct.pack_into("<H", archive_bytes, central_offset + 32, len(locator))
    archive_bytes[original_eocd:original_eocd] = locator
    eocd_offset = original_eocd + len(locator)
    struct.pack_into(
        "<I",
        archive_bytes,
        eocd_offset + 12,
        central_size + len(locator),
    )
    result.zip_path.write_bytes(archive_bytes)

    def forbidden_zipfile(*args: object, **kwargs: object) -> object:
        raise AssertionError("ZipFile constructed before physical ZIP64 detection")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden_zipfile)
    with pytest.raises(UnsafeArchiveError, match="ZIP64"):
        verify_deterministic_zip(result.zip_path)


def test_physical_zip64_locator_is_rejected_without_classic_sentinels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print('safe')\n", encoding="utf-8")
    result = DeterministicZipBuilder(includes=("**/*",)).build(
        source,
        tmp_path / "unexpected-zip64.zip",
    )
    archive_bytes = bytearray(result.zip_path.read_bytes())
    original_eocd = len(archive_bytes) - 22
    member_count = struct.unpack_from("<H", archive_bytes, original_eocd + 10)[0]
    central_size = struct.unpack_from("<I", archive_bytes, original_eocd + 12)[0]
    central_offset = struct.unpack_from("<I", archive_bytes, original_eocd + 16)[0]
    zip64_record = struct.pack(
        "<4sQHHIIQQQQ",
        b"PK\x06\x06",
        44,
        45,
        45,
        0,
        0,
        member_count,
        member_count,
        central_size,
        central_offset,
    )
    locator = struct.pack(
        "<4sIQI",
        b"PK\x06\x07",
        0,
        original_eocd,
        1,
    )
    archive_bytes[original_eocd:original_eocd] = zip64_record + locator
    result.zip_path.write_bytes(archive_bytes)

    def forbidden_zipfile(*args: object, **kwargs: object) -> object:
        raise AssertionError("ZipFile constructed before unexpected ZIP64 rejection")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden_zipfile)
    with pytest.raises(UnsafeArchiveError, match="unexpected physical ZIP64"):
        verify_deterministic_zip(result.zip_path)
