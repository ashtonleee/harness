import json
from pathlib import Path

import pytest

from trusted.recovery.store import WorkspaceRecoveryStore
from trusted.state.store import TrustedStateManager


def write_tree(root: Path, files: dict[str, str]):
    for relative_path, content in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="ascii")


def test_workspace_recovery_store_creates_restores_and_resets(tmp_path):
    baseline_dir = tmp_path / "baseline"
    workspace_dir = tmp_path / "workspace"
    recovery_dir = tmp_path / "trusted_state" / "checkpoints"

    write_tree(
        baseline_dir,
        {
            "README.md": "# baseline\n",
            "seedlib.py": "def value():\n    return 'baseline'\n",
            "tests/test_seedlib.py": "from seedlib import value\n\n\ndef test_value():\n    assert value() == 'baseline'\n",
        },
    )
    write_tree(
        workspace_dir,
        {
            "README.md": "# baseline\n",
            "seedlib.py": "def value():\n    return 'baseline'\n",
            "tests/test_seedlib.py": "from seedlib import value\n\n\ndef test_value():\n    assert value() == 'baseline'\n",
        },
    )

    store = WorkspaceRecoveryStore(
        workspace_dir=workspace_dir,
        recovery_dir=recovery_dir,
        baseline_source_dir=baseline_dir,
    )
    layout = store.ensure_layout()
    assert layout["baseline"]["baseline_id"]
    assert Path(layout["baseline"]["archive_path"]).is_file()
    assert str(recovery_dir) not in str(workspace_dir)

    workspace_dir.joinpath("seedlib.py").write_text(
        "def value():\n    return 'edited'\n",
        encoding="ascii",
    )
    checkpoint = store.create_checkpoint(label="after-edit")
    assert checkpoint["checkpoint_id"]
    assert checkpoint["label"] == "after-edit"
    assert Path(checkpoint["archive_path"]).is_file()
    assert Path(checkpoint["manifest_path"]).is_file()
    assert Path(checkpoint["archive_path"]).is_relative_to(recovery_dir)
    assert store.list_checkpoints()[0]["checkpoint_id"] == checkpoint["checkpoint_id"]

    workspace_dir.joinpath("seedlib.py").write_text(
        "def value():\n    return 'broken'\n",
        encoding="ascii",
    )
    workspace_dir.joinpath("scratch.txt").write_text("temp\n", encoding="ascii")

    restored = store.restore_checkpoint(checkpoint["checkpoint_id"])
    assert restored["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert workspace_dir.joinpath("seedlib.py").read_text(encoding="ascii").endswith(
        "return 'edited'\n"
    )
    assert not workspace_dir.joinpath("scratch.txt").exists()

    workspace_dir.joinpath("extra.py").write_text("x = 1\n", encoding="ascii")
    reset = store.reset_to_seed_baseline()
    assert reset["baseline_id"] == layout["baseline"]["baseline_id"]
    assert workspace_dir.joinpath("seedlib.py").read_text(encoding="ascii").endswith(
        "return 'baseline'\n"
    )
    assert not workspace_dir.joinpath("extra.py").exists()

    summary = store.current_recovery_summary()
    assert summary["latest_checkpoint_id"] == checkpoint["checkpoint_id"]
    assert summary["current_workspace_status"] == "seed_baseline"
    assert summary["latest_action"]["action"] == "workspace_reset"


def test_workspace_recovery_store_rejects_invalid_checkpoint_id(tmp_path):
    baseline_dir = tmp_path / "baseline"
    workspace_dir = tmp_path / "workspace"
    recovery_dir = tmp_path / "trusted_state" / "checkpoints"
    write_tree(baseline_dir, {"seedlib.py": "x = 1\n"})
    write_tree(workspace_dir, {"seedlib.py": "x = 1\n"})

    store = WorkspaceRecoveryStore(
        workspace_dir=workspace_dir,
        recovery_dir=recovery_dir,
        baseline_source_dir=baseline_dir,
    )
    store.ensure_layout()

    with pytest.raises(FileNotFoundError):
        store.restore_checkpoint("missing-checkpoint")


def test_trusted_state_manager_materializes_recovery_events(tmp_path):
    log_path = tmp_path / "logs" / "bridge_events.jsonl"
    state_path = tmp_path / "state" / "operational_state.json"
    recovery_dir = tmp_path / "checkpoints"
    manager = TrustedStateManager(
        canonical_log_path=log_path,
        operational_state_path=state_path,
        budget_total=30,
        budget_unit="mock_tokens",
        stage="stage4_workspace_recovery",
        surfaces={
            "canonical_logging": "active_canonical_event_log",
            "budgeting": "enforced_token_cap_stage2",
            "recovery": "trusted_host_checkpoint_controls_stage4",
        },
        recovery_defaults={
            "checkpoint_dir": str(recovery_dir),
            "baseline_id": "baseline-123",
            "baseline_source_dir": "/trusted/recovery/seed_workspace_baseline",
            "baseline_archive_path": str(recovery_dir / "baselines" / "seed-baseline.tar.gz"),
            "available_checkpoints": [],
            "latest_checkpoint_id": None,
            "latest_action": None,
            "current_workspace_status": "seed_baseline",
        },
    )

    manager.append_event(
        event_type="checkpoint_created",
        actor="operator",
        source_service="operator_recovery_cli",
        request_id="req-checkpoint",
        trace_id="trace-checkpoint",
        outcome="success",
        summary={
            "checkpoint": {
                "checkpoint_id": "ckpt-1",
                "label": "before-change",
                "created_at": "2026-03-12T00:00:00+00:00",
                "archive_path": str(recovery_dir / "archives" / "ckpt-1.tar.gz"),
                "manifest_path": str(recovery_dir / "manifests" / "ckpt-1.json"),
            },
            "recovery": {
                "available_checkpoints": [
                    {
                        "checkpoint_id": "ckpt-1",
                        "label": "before-change",
                        "created_at": "2026-03-12T00:00:00+00:00",
                        "archive_path": str(recovery_dir / "archives" / "ckpt-1.tar.gz"),
                        "manifest_path": str(recovery_dir / "manifests" / "ckpt-1.json"),
                    }
                ],
                "latest_checkpoint_id": "ckpt-1",
                "latest_action": {
                    "action": "checkpoint_created",
                    "checkpoint_id": "ckpt-1",
                    "timestamp": "2026-03-12T00:00:00+00:00",
                    "outcome": "success",
                },
                "current_workspace_status": "checkpoint:ckpt-1",
            },
        },
    )
    manager.append_event(
        event_type="workspace_reset",
        actor="operator",
        source_service="operator_recovery_cli",
        request_id="req-reset",
        trace_id="trace-reset",
        outcome="success",
        summary={
            "baseline_id": "baseline-123",
            "recovery": {
                "available_checkpoints": [
                    {
                        "checkpoint_id": "ckpt-1",
                        "label": "before-change",
                        "created_at": "2026-03-12T00:00:00+00:00",
                        "archive_path": str(recovery_dir / "archives" / "ckpt-1.tar.gz"),
                        "manifest_path": str(recovery_dir / "manifests" / "ckpt-1.json"),
                    }
                ],
                "latest_checkpoint_id": "ckpt-1",
                "latest_action": {
                    "action": "workspace_reset",
                    "baseline_id": "baseline-123",
                    "timestamp": "2026-03-12T00:01:00+00:00",
                    "outcome": "success",
                },
                "current_workspace_status": "seed_baseline",
            },
        },
    )

    snapshot = manager.snapshot()
    assert snapshot["recovery"]["baseline_id"] == "baseline-123"
    assert snapshot["recovery"]["latest_checkpoint_id"] == "ckpt-1"
    assert snapshot["recovery"]["current_workspace_status"] == "seed_baseline"
    assert snapshot["recovery"]["latest_action"]["action"] == "workspace_reset"
    assert snapshot["counters"]["checkpoint_events"] == 2

    reloaded = TrustedStateManager(
        canonical_log_path=log_path,
        operational_state_path=state_path,
        budget_total=30,
        budget_unit="mock_tokens",
        stage="stage4_workspace_recovery",
        surfaces={
            "canonical_logging": "active_canonical_event_log",
            "budgeting": "enforced_token_cap_stage2",
            "recovery": "trusted_host_checkpoint_controls_stage4",
        },
        recovery_defaults={
            "checkpoint_dir": str(recovery_dir),
            "baseline_id": "baseline-123",
            "baseline_source_dir": "/trusted/recovery/seed_workspace_baseline",
            "baseline_archive_path": str(recovery_dir / "baselines" / "seed-baseline.tar.gz"),
            "available_checkpoints": [],
            "latest_checkpoint_id": None,
            "latest_action": None,
            "current_workspace_status": "seed_baseline",
        },
    )
    assert reloaded.snapshot()["recovery"]["latest_action"]["action"] == "workspace_reset"
    assert json.loads(state_path.read_text(encoding="ascii"))["recovery"]["baseline_id"] == "baseline-123"
