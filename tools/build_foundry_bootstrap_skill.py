"""Compatibility notice for the retired Python bootstrap release builder."""

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = (
    REPOSITORY_ROOT
    / ".github"
    / "workflows"
    / "release-foundry-bootstrap-skill.yml"
)


def main() -> None:
    if not RELEASE_WORKFLOW.is_file():
        raise SystemExit(f"missing static release workflow: {RELEASE_WORKFLOW}")
    print(
        "foundry-bootstrap is packaged by "
        ".github/workflows/release-foundry-bootstrap-skill.yml"
    )


if __name__ == "__main__":
    main()
