import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import shutil
import tarfile
from typing import Any
from uuid import uuid4


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json_atomic(path: Path, payload: dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="ascii") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    tmp_path.replace(path)


def _iter_files(root: Path):
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def _snapshot_metadata(root: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    file_count = 0
    size_bytes = 0
    for path in _iter_files(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        data = path.read_bytes()
        digest.update(data)
        digest.update(b"\0")
        file_count += 1
        size_bytes += len(data)
    return {
        "workspace_digest": digest.hexdigest(),
        "file_count": file_count,
        "size_bytes": size_bytes,
    }


def _write_archive(source_dir: Path, archive_path: Path):
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "w:gz") as archive:
        for path in _iter_files(source_dir):
            archive.add(path, arcname=path.relative_to(source_dir).as_posix())


def _safe_extract(archive_path: Path, target_dir: Path):
    target_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            resolved = (target_dir / member.name).resolve()
            if not resolved.is_relative_to(target_dir.resolve()):
                raise ValueError(f"archive path escapes workspace: {member.name}")
        archive.extractall(target_dir)


def _clear_directory(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    for child in root.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


@dataclass
class WorkspaceRecoveryStore:
    recovery_dir: Path
    baseline_source_dir: Path
    workspace_dir: Path | None = None

    def __post_init__(self):
        self.recovery_dir = self.recovery_dir.resolve()
        self.baseline_source_dir = self.baseline_source_dir.resolve()
        self.workspace_dir = self.workspace_dir.resolve() if self.workspace_dir else None
        self.archives_dir = self.recovery_dir / "archives"
        self.manifests_dir = self.recovery_dir / "manifests"
        self.baselines_dir = self.recovery_dir / "baselines"
        self._latest_action: dict[str, Any] | None = None
        self._current_workspace_status = "seed_baseline"

    def _require_workspace_dir(self) -> Path:
        if self.workspace_dir is None:
            raise RuntimeError("workspace access is not available in this context")
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        return self.workspace_dir

    def _baseline_paths(self) -> tuple[Path, Path]:
        archive_path = self.baselines_dir / "seed_workspace_baseline.tar.gz"
        manifest_path = self.baselines_dir / "seed_workspace_baseline.json"
        return archive_path, manifest_path

    def baseline_metadata(self) -> dict[str, Any]:
        archive_path, manifest_path = self._baseline_paths()
        metadata = _snapshot_metadata(self.baseline_source_dir)
        baseline_id = f"seed-{metadata['workspace_digest'][:12]}"
        payload = {
            "baseline_id": baseline_id,
            "created_at": utc_now_iso(),
            "archive_path": str(archive_path),
            "manifest_path": str(manifest_path),
            "baseline_source_dir": str(self.baseline_source_dir),
            **metadata,
        }
        return payload

    def ensure_layout(self) -> dict[str, Any]:
        self.recovery_dir.mkdir(parents=True, exist_ok=True)
        self.archives_dir.mkdir(parents=True, exist_ok=True)
        self.manifests_dir.mkdir(parents=True, exist_ok=True)
        self.baselines_dir.mkdir(parents=True, exist_ok=True)

        baseline = self.baseline_metadata()
        archive_path = Path(baseline["archive_path"])
        manifest_path = Path(baseline["manifest_path"])
        manifest_payload = None
        if manifest_path.exists():
            manifest_payload = json.loads(manifest_path.read_text(encoding="ascii"))

        if (
            not archive_path.exists()
            or manifest_payload is None
            or manifest_payload.get("baseline_id") != baseline["baseline_id"]
        ):
            _write_archive(self.baseline_source_dir, archive_path)
            _write_json_atomic(manifest_path, baseline)
            manifest_payload = baseline

        return {
            "checkpoint_dir": str(self.recovery_dir),
            "baseline": manifest_payload,
        }

    def recovery_defaults(self) -> dict[str, Any]:
        layout = self.ensure_layout()
        baseline = layout["baseline"]
        return {
            "checkpoint_dir": layout["checkpoint_dir"],
            "baseline_id": baseline["baseline_id"],
            "baseline_source_dir": baseline["baseline_source_dir"],
            "baseline_archive_path": baseline["archive_path"],
            "available_checkpoints": self.list_checkpoints(),
            "latest_checkpoint_id": self.list_checkpoints()[0]["checkpoint_id"]
            if self.list_checkpoints()
            else None,
            "latest_action": self._latest_action,
            "current_workspace_status": self._current_workspace_status,
        }

    def list_checkpoints(self) -> list[dict[str, Any]]:
        self.ensure_layout()
        checkpoints: list[dict[str, Any]] = []
        for path in sorted(self.manifests_dir.glob("*.json"), reverse=True):
            payload = json.loads(path.read_text(encoding="ascii"))
            checkpoints.append(payload)
        return checkpoints

    def _checkpoint_paths(self, checkpoint_id: str) -> tuple[Path, Path]:
        return (
            self.archives_dir / f"{checkpoint_id}.tar.gz",
            self.manifests_dir / f"{checkpoint_id}.json",
        )

    def create_checkpoint(self, *, label: str | None = None) -> dict[str, Any]:
        workspace_dir = self._require_workspace_dir()
        self.ensure_layout()
        timestamp = utc_now_iso()
        checkpoint_id = f"ckpt-{timestamp.replace(':', '').replace('-', '').replace('+00:00', 'z')}-{uuid4().hex[:8]}"
        archive_path, manifest_path = self._checkpoint_paths(checkpoint_id)
        _write_archive(workspace_dir, archive_path)
        metadata = _snapshot_metadata(workspace_dir)
        payload = {
            "checkpoint_id": checkpoint_id,
            "created_at": timestamp,
            "archive_path": str(archive_path),
            "manifest_path": str(manifest_path),
            "label": label,
            **metadata,
        }
        _write_json_atomic(manifest_path, payload)
        self._latest_action = {
            "action": "checkpoint_created",
            "timestamp": timestamp,
            "outcome": "success",
            "checkpoint_id": checkpoint_id,
        }
        self._current_workspace_status = f"checkpoint:{checkpoint_id}"
        return payload

    def restore_checkpoint(self, checkpoint_id: str) -> dict[str, Any]:
        workspace_dir = self._require_workspace_dir()
        self.ensure_layout()
        archive_path, manifest_path = self._checkpoint_paths(checkpoint_id)
        if not archive_path.exists() or not manifest_path.exists():
            raise FileNotFoundError(f"unknown checkpoint_id: {checkpoint_id}")
        _clear_directory(workspace_dir)
        _safe_extract(archive_path, workspace_dir)
        manifest = json.loads(manifest_path.read_text(encoding="ascii"))
        self._latest_action = {
            "action": "checkpoint_restored",
            "timestamp": utc_now_iso(),
            "outcome": "success",
            "checkpoint_id": checkpoint_id,
        }
        self._current_workspace_status = f"checkpoint:{checkpoint_id}"
        return manifest

    def reset_to_seed_baseline(self) -> dict[str, Any]:
        workspace_dir = self._require_workspace_dir()
        layout = self.ensure_layout()
        baseline = layout["baseline"]
        _clear_directory(workspace_dir)
        _safe_extract(Path(baseline["archive_path"]), workspace_dir)
        self._latest_action = {
            "action": "workspace_reset",
            "timestamp": utc_now_iso(),
            "outcome": "success",
            "baseline_id": baseline["baseline_id"],
        }
        self._current_workspace_status = "seed_baseline"
        return baseline

    def current_recovery_summary(self) -> dict[str, Any]:
        defaults = self.recovery_defaults()
        defaults["latest_action"] = self._latest_action
        defaults["current_workspace_status"] = self._current_workspace_status
        return defaults
