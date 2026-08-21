from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from foundry_opt.packaging.foundry_bootstrap_release import (  # noqa: E402
    build_foundry_bootstrap_skill,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build deterministic foundry-bootstrap skill release artifacts."
    )
    parser.add_argument(
        "--repository-root",
        default=str(REPOSITORY_ROOT),
        help="Repository checkout root. Defaults to this repository.",
    )
    parser.add_argument(
        "--dist-root",
        help="Output directory for the unpacked package, ZIP, and checksum manifest.",
    )
    parser.add_argument(
        "--runtime-repository",
        help="Override the runtime repository URL. Defaults to remote.origin.url.",
    )
    parser.add_argument(
        "--runtime-commit",
        help="Override the runtime commit SHA. Defaults to HEAD.",
    )
    parser.add_argument(
        "--package-path",
        default=".",
        help="Repository-relative runtime package path. Defaults to '.'.",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    result = build_foundry_bootstrap_skill(
        args.repository_root,
        dist_root=args.dist_root,
        runtime_repository=args.runtime_repository,
        runtime_commit=args.runtime_commit,
        package_path=args.package_path,
    )
    print(f"package_dir={result.package_directory}")
    print(f"zip={result.zip_path}")
    print(f"manifest={result.manifest_path}")
    print(f"zip_sha256={result.zip_sha256}")
    print(f"runtime_commit={result.runtime.runtime_commit}")


if __name__ == "__main__":
    main()
