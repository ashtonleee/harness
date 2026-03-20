import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.fast


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def copy_script(repo_root: Path, relative_path: str) -> Path:
    source = ROOT / relative_path
    target = repo_root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    target.chmod(0o755)
    return target


def init_git_repo(repo_root: Path) -> str:
    run(["git", "init"], cwd=repo_root)
    run(["git", "config", "user.name", "Codex Tests"], cwd=repo_root)
    run(["git", "config", "user.email", "codex-tests@example.com"], cwd=repo_root)
    run(["git", "add", "."], cwd=repo_root)
    commit = run(["git", "commit", "-m", "baseline"], cwd=repo_root)
    assert commit.returncode == 0, commit.stderr
    result = run(["git", "rev-parse", "HEAD"], cwd=repo_root)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def write_python_shim(
    path: Path,
    *,
    real_python: str,
    label: str,
    fail_yaml: bool = False,
    fast_rc: int | None = None,
    unit_rc: int | None = None,
) -> None:
    fail_yaml_block = "echo 'missing yaml' >&2\n    exit 1" if fail_yaml else f'exec "{real_python}" "$@"'
    fast_block = f"exit {fast_rc}" if fast_rc is not None else f'exec "{real_python}" -m pytest "$@"'
    unit_block = f"exit {unit_rc}" if unit_rc is not None else f'exec "{real_python}" -m pytest "$@"'
    shim = textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        set -euo pipefail
        label={label!r}
        log=${{PYTHON_SHIM_LOG:-}}
        if [[ -n "$log" ]]; then
            printf '%s:%s\\n' "$label" "$*" >> "$log"
        fi
        if [[ "${{1-}}" == "-c" ]]; then
            code="${{2-}}"
            if [[ "$code" == *"import yaml"* ]]; then
                {fail_yaml_block}
            fi
        fi
        if [[ "${{1-}}" == "-m" && "${{2-}}" == "pytest" ]]; then
            shift 2
            args="$*"
            if [[ "$args" == "-m fast -q --tb=short" ]]; then
                {fast_block}
            fi
            if [[ "$args" == "tests/unit/ -q --tb=short" ]]; then
                {unit_block}
            fi
            exec "{real_python}" -m pytest "$@"
        fi
        exec "{real_python}" "$@"
        """
    )
    path.write_text(shim, encoding="ascii")
    path.chmod(0o755)


def make_preflight_repo(tmp_path: Path, *, fast_rc: int, unit_rc: int) -> tuple[Path, dict[str, str]]:
    repo_root = tmp_path / "preflight_repo"
    repo_root.mkdir()
    copy_script(repo_root, "scripts/preflight.sh")

    (repo_root / "STAGE_STATUS.md").write_text(
        "# STAGE_STATUS.md\n\n## Session Log\n\n| Date | Agent | Summary |\n|------|-------|---------|\n",
        encoding="ascii",
    )
    (repo_root / "TASK_GRAPH.md").write_text("# TASK_GRAPH.md\n", encoding="ascii")
    (repo_root / "ACCEPTANCE_TEST_MATRIX.md").write_text("# ACCEPTANCE_TEST_MATRIX.md\n", encoding="ascii")
    (repo_root / "REPO_LAYOUT.md").write_text("# REPO_LAYOUT.md\n", encoding="ascii")
    (repo_root / "plans").mkdir()
    (repo_root / "plans" / "INDEX.md").write_text("# Plan Index\n", encoding="ascii")
    (repo_root / "assurance").mkdir()
    (repo_root / "assurance" / "REGISTRY.yaml").write_text(
        "schema_version: 1\ncomponents:\n  - id: demo\n    watch_paths:\n      - README.md\n",
        encoding="ascii",
    )
    (repo_root / "README.md").write_text("# demo\n", encoding="ascii")

    bin_dir = repo_root / "bin"
    bin_dir.mkdir()
    write_python_shim(
        bin_dir / "python3",
        real_python=sys.executable,
        label="python3",
        fast_rc=fast_rc,
        unit_rc=unit_rc,
    )
    shutil.copy2(bin_dir / "python3", bin_dir / "python")

    init_git_repo(repo_root)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return repo_root, env


def make_staleness_repo(tmp_path: Path, *, reviewed_commit: str) -> tuple[Path, dict[str, str], str]:
    repo_root = tmp_path / "staleness_repo"
    repo_root.mkdir()
    copy_script(repo_root, "scripts/staleness.sh")
    (repo_root / "assurance").mkdir()
    (repo_root / "README.md").write_text("baseline\n", encoding="ascii")
    (repo_root / "assurance" / "REGISTRY.yaml").write_text(
        "schema_version: 1\n"
        f"reviewed_commit: {reviewed_commit}\n"
        "components:\n"
        "  - id: scope\n"
        "    title: Scope\n"
        "    watch_paths:\n"
        "      - README.md\n",
        encoding="ascii",
    )
    commit = init_git_repo(repo_root)
    env = os.environ.copy()
    return repo_root, env, commit


def test_preflight_fails_when_full_unit_suite_is_red(tmp_path: Path):
    repo_root, env = make_preflight_repo(tmp_path, fast_rc=0, unit_rc=1)
    result = run(["bash", "scripts/preflight.sh"], cwd=repo_root, env=env)
    assert result.returncode == 1
    assert "PASS: Fast-marked unit tests" in result.stdout
    assert "FAIL: All unit tests" in result.stdout


def test_preflight_passes_when_fast_and_full_unit_suites_are_green(tmp_path: Path):
    repo_root, env = make_preflight_repo(tmp_path, fast_rc=0, unit_rc=0)
    result = run(["bash", "scripts/preflight.sh"], cwd=repo_root, env=env)
    assert result.returncode == 0
    assert "PASS: All unit tests" in result.stdout
    assert "All preflight checks passed." in result.stdout


def test_staleness_fails_on_invalid_reviewed_commit(tmp_path: Path):
    repo_root, env, _ = make_staleness_repo(tmp_path, reviewed_commit="pending_post_review_fixes")
    result = run(["bash", "scripts/staleness.sh"], cwd=repo_root, env=env)
    assert result.returncode == 1
    assert "reviewed_commit 'pending_post_review_fixes' is not a valid commit" in result.stderr


def test_staleness_reports_stale_components_for_watched_file_diff(tmp_path: Path):
    repo_root, env, commit = make_staleness_repo(tmp_path, reviewed_commit="placeholder")
    registry = repo_root / "assurance" / "REGISTRY.yaml"
    registry.write_text(
        registry.read_text(encoding="ascii").replace("placeholder", commit),
        encoding="ascii",
    )
    (repo_root / "README.md").write_text("changed\n", encoding="ascii")

    result = run(["bash", "scripts/staleness.sh"], cwd=repo_root, env=env)
    assert result.returncode == 1
    assert "scope (Scope)" in result.stdout
    assert "- README.md" in result.stdout


def test_staleness_reports_fresh_when_no_watched_files_changed(tmp_path: Path):
    repo_root, env, commit = make_staleness_repo(tmp_path, reviewed_commit="placeholder")
    registry = repo_root / "assurance" / "REGISTRY.yaml"
    registry.write_text(
        registry.read_text(encoding="ascii").replace("placeholder", commit),
        encoding="ascii",
    )

    result = run(["bash", "scripts/staleness.sh"], cwd=repo_root, env=env)
    assert result.returncode == 0
    assert f"Reviewed commit: {commit}" in result.stdout
    assert "All assurance components are FRESH." in result.stdout


def test_staleness_prefers_yaml_capable_python_later_in_path(tmp_path: Path):
    repo_root, env, commit = make_staleness_repo(tmp_path, reviewed_commit="placeholder")
    registry = repo_root / "assurance" / "REGISTRY.yaml"
    registry.write_text(
        registry.read_text(encoding="ascii").replace("placeholder", commit),
        encoding="ascii",
    )

    bin_dir = repo_root / "bin"
    bin_dir.mkdir()
    log_path = repo_root / "python.log"
    env["PYTHON_SHIM_LOG"] = str(log_path)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"

    write_python_shim(
        bin_dir / "python3",
        real_python=sys.executable,
        label="python3",
        fail_yaml=True,
    )
    write_python_shim(
        bin_dir / "python",
        real_python=sys.executable,
        label="python",
    )

    result = run(["bash", "scripts/staleness.sh"], cwd=repo_root, env=env)
    assert result.returncode == 0, result.stderr
    log_lines = log_path.read_text(encoding="ascii").splitlines()
    assert any(line.startswith("python3:-c import yaml") for line in log_lines)
    assert any(line.startswith("python:-c import yaml") for line in log_lines)


def test_staleness_reports_untracked_files_under_watched_paths(tmp_path: Path):
    repo_root, env, commit = make_staleness_repo(tmp_path, reviewed_commit="placeholder")
    registry = repo_root / "assurance" / "REGISTRY.yaml"
    registry.write_text(
        "schema_version: 1\n"
        f"reviewed_commit: {commit}\n"
        "components:\n"
        "  - id: scope\n"
        "    title: Scope\n"
        "    watch_paths:\n"
        "      - scratch/*\n",
        encoding="ascii",
    )

    scratch_dir = repo_root / "scratch"
    scratch_dir.mkdir()
    (scratch_dir / "new.txt").write_text("untracked\n", encoding="ascii")

    result = run(["bash", "scripts/staleness.sh"], cwd=repo_root, env=env)
    assert result.returncode == 1
    assert "scope (Scope)" in result.stdout
    assert "- scratch/new.txt" in result.stdout
