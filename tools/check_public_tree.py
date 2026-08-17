from __future__ import annotations

import subprocess
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".ini",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
FORBIDDEN = (
    "luechen@microsoft.com",
    "XOEEst/foundry-cloud-coding-agents-002",
    "microsoft-foundry/luffy",
    "luffy-test-agent-repo-002",
    r"C:\Users\luechen",
    r"Q:\GIT\XOEEst",
)


def _tracked_paths() -> tuple[Path, ...]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z"],
        cwd=REPOSITORY_ROOT,
    )
    return tuple(
        REPOSITORY_ROOT / value.decode("utf-8")
        for value in output.split(b"\0")
        if value
    )


def main() -> None:
    violations: list[str] = []
    for path in _tracked_paths():
        relative = path.relative_to(REPOSITORY_ROOT).as_posix()
        if relative.startswith("tests/") or relative == "tools/check_public_tree.py":
            continue
        if path.suffix.casefold() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for forbidden in FORBIDDEN:
            if forbidden.casefold() in text.casefold():
                violations.append(f"{relative}: contains {forbidden!r}")
    if violations:
        raise SystemExit("\n".join(violations))


if __name__ == "__main__":
    main()
