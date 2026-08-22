from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INLINE_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
REFERENCE_LINK = re.compile(r"^\s*\[[^\]]+\]:\s*(\S+)", re.MULTILINE)
FORBIDDEN = (
    "luechen@microsoft.com",
    "XOEEst/foundry-cloud-coding-agents-002",
    "microsoft-foundry/luffy",
    "luffy-test-agent-repo-002",
    r"C:\Users\luechen",
    r"Q:\GIT\XOEEst",
)
REQUIRED_INDEX_LINKS = (
    "architecture/system-overview.md",
    "architecture/module-map.md",
    "architecture/skill-runtime-seam.md",
    "architecture/optimize-job.md",
    "architecture/deployment.md",
    "architecture/trust-model.md",
    "guides/run-an-optimization.md",
    "guides/operate-deployments.md",
    "reference/cli.md",
    "reference/repository-contract.md",
    "reference/evidence-state-and-receipts.md",
    "decisions/README.md",
)


def _working_tree_paths() -> tuple[Path, ...]:
    output = subprocess.check_output(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        cwd=REPOSITORY_ROOT,
    )
    return tuple(
        REPOSITORY_ROOT / value.decode("utf-8")
        for value in output.split(b"\0")
        if value
    )


def _tracked_names(paths: tuple[Path, ...]) -> tuple[set[str], set[str]]:
    files = {path.relative_to(REPOSITORY_ROOT).as_posix() for path in paths}
    directories: set[str] = set()
    for name in files:
        parent = Path(name).parent
        while parent != Path("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return files, directories


def _destinations(text: str) -> tuple[str, ...]:
    values = [match.group(1).strip() for match in INLINE_LINK.finditer(text)]
    values.extend(match.group(1).strip() for match in REFERENCE_LINK.finditer(text))
    return tuple(values)


def _link_target(destination: str) -> str | None:
    if destination.startswith("<") and ">" in destination:
        destination = destination[1 : destination.index(">")]
    else:
        destination = destination.split(maxsplit=1)[0]
    destination = unquote(destination.strip())
    if not destination or destination.startswith("#"):
        return None
    parsed = urlparse(destination)
    if parsed.scheme or destination.startswith("//"):
        return None
    return destination.split("#", 1)[0].split("?", 1)[0]


def _relative_name(path: Path, target: str) -> str | None:
    if target.startswith("/"):
        candidate = REPOSITORY_ROOT / target.lstrip("/")
    else:
        candidate = path.parent / target
    try:
        return candidate.resolve().relative_to(REPOSITORY_ROOT.resolve()).as_posix()
    except ValueError:
        return None


def collect_violations() -> list[str]:
    working_tree_paths = _working_tree_paths()
    paths = tuple(path for path in working_tree_paths if path.suffix.casefold() == ".md")
    tracked_files, tracked_directories = _tracked_names(working_tree_paths)
    violations: list[str] = []

    for path in paths:
        relative = path.relative_to(REPOSITORY_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        if relative == "docs/README.md":
            for required in REQUIRED_INDEX_LINKS:
                if f"]({required})" not in text:
                    violations.append(f"{relative}: missing index link {required}")
        if relative == "docs/retained-pilot.md" and "illustrative" not in text.casefold():
            violations.append(f"{relative}: retained evidence must be labeled illustrative")
        if relative == "README.md" or relative.startswith(("docs/", "plugins/")):
            for forbidden in FORBIDDEN:
                if forbidden.casefold() in text.casefold():
                    violations.append(f"{relative}: contains {forbidden!r}")

        for destination in _destinations(text):
            target = _link_target(destination)
            if target is None:
                continue
            name = _relative_name(path, target)
            if name is None:
                violations.append(f"{relative}: link escapes repository: {destination}")
                continue
            if name not in tracked_files and name not in tracked_directories:
                violations.append(f"{relative}: unresolved link: {destination}")

    return violations


def main() -> None:
    violations = collect_violations()
    if violations:
        raise SystemExit("\n".join(violations))


if __name__ == "__main__":
    main()
