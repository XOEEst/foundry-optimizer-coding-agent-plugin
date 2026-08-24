from __future__ import annotations

from pathlib import Path

import yaml

from foundry_opt.repository_selection import build_changed_path_matrix


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = (
    REPOSITORY_ROOT / "src" / "foundry_opt" / "templates" / "customer-repo"
)


def test_bootstrap_report_change_does_not_redeploy_agents(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    sidecar = repository / "agent" / ".foundry" / "foundry-opt.yaml"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(
        (
            TEMPLATE_ROOT / "agent" / ".foundry" / "foundry-opt.yaml"
        ).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    registry = yaml.safe_load(
        (
            TEMPLATE_ROOT / ".foundry-opt" / "registry.yaml"
        ).read_text(encoding="utf-8")
    )
    registry_path = repository / ".foundry-opt" / "registry.yaml"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        yaml.safe_dump(registry, sort_keys=False),
        encoding="utf-8",
    )

    matrix = build_changed_path_matrix(
        repository,
        changed_paths=[".foundry-opt/bootstrap-report.md"],
    )

    assert matrix == ()
