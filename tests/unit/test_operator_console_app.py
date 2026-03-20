import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from operator_console.app import create_app
from operator_console.bridge_api import BridgeNotFoundError, BridgeUnavailableError
from operator_console.config import ConsoleSettings
from operator_console.data import RepoData
from shared.schemas import (
    BridgeStatusReport,
    BrowserState,
    BudgetState,
    ConnectionStatus,
    ProposalRecord,
    ProposalState,
    RecentRequest,
    RecoveryState,
    WebState,
)


class FakeBridgeAPI:
    def __init__(
        self,
        *,
        status: BridgeStatusReport | None = None,
        proposals: list[ProposalRecord] | None = None,
        error: str | None = None,
    ):
        self._status = status
        self._proposals = proposals or []
        self._error = error

    async def get_status(self) -> BridgeStatusReport:
        if self._error:
            raise BridgeUnavailableError(self._error)
        assert self._status is not None
        return self._status

    async def list_proposals(self, *, status: str | None = None) -> list[ProposalRecord]:
        if self._error:
            raise BridgeUnavailableError(self._error)
        proposals = self._proposals
        if status:
            proposals = [proposal for proposal in proposals if proposal.status == status]
        return proposals

    async def get_proposal(self, proposal_id: str) -> ProposalRecord:
        if self._error:
            raise BridgeUnavailableError(self._error)
        for proposal in self._proposals:
            if proposal.proposal_id == proposal_id:
                return proposal
        raise BridgeNotFoundError("proposal not found")


def make_settings(tmp_path: Path) -> ConsoleSettings:
    workspace_dir = tmp_path / "agent_workspace"
    (workspace_dir / "run_outputs").mkdir(parents=True)
    (workspace_dir / "research").mkdir()
    trusted_state_dir = tmp_path / "trusted_state"
    (trusted_state_dir / "logs").mkdir(parents=True)
    return ConsoleSettings(
        bridge_url="http://127.0.0.1:8000",
        operator_token="token",
        workspace_dir=workspace_dir,
        trusted_state_dir=trusted_state_dir,
    )


def write_demo_files(settings: ConsoleSettings) -> None:
    (settings.workspace_dir / "run_outputs" / "latest_seed_run.json").write_text(
        json.dumps(
            {
                "task": "read site",
                "success": True,
                "finished_reason": "planner_finished",
                "steps_executed": 2,
                "steps": [
                    {"step_index": 0, "kind": "bridge_status", "params": {}, "result": {"stage": "stage8"}},
                    {"step_index": 1, "kind": "write_file", "params": {"path": "research/current_real_site_brief.md"}, "result": {"bytes_written": 10}},
                ],
            }
        ),
        encoding="utf-8",
    )
    (settings.workspace_dir / "research" / "current_real_site_brief.md").write_text(
        "# Brief\n\n- item one\n",
        encoding="utf-8",
    )
    (settings.workspace_dir / "research" / "current_real_site_screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\n")


def make_status() -> BridgeStatusReport:
    connection = ConnectionStatus(
        url="http://service",
        reachable=True,
        detail=None,
        checked_at="2026-03-19T22:00:00+00:00",
    )
    return BridgeStatusReport(
        service="bridge",
        stage="stage8_consequential_actions",
        trusted_state_dir="/var/lib/rsi/trusted_state",
        log_path="/var/lib/rsi/trusted_state/logs/bridge_events.jsonl",
        operational_state_path="/var/lib/rsi/trusted_state/state/operational_state.json",
        connections={
            "litellm": connection,
            "fetcher": connection,
            "browser": connection,
        },
        budget=BudgetState(
            unit="mock_tokens",
            total=100,
            spent=20,
            remaining=80,
            exhausted=False,
            minimum_call_cost=5,
            approximation="deterministic",
            total_prompt_tokens=10,
            total_completion_tokens=10,
            total_tokens=20,
        ),
        recovery=RecoveryState(
            checkpoint_dir="/tmp/checkpoints",
            baseline_id="baseline-1",
            baseline_source_dir="/tmp/source",
            baseline_archive_path="/tmp/archive",
            available_checkpoints=[],
            latest_checkpoint_id=None,
            latest_action=None,
            current_workspace_status="seed_baseline",
        ),
        web=WebState(
            fetcher=connection,
            allowlist_hosts=["httpbin.org"],
            private_test_hosts=[],
            allowed_content_types=["text/html"],
            caps={"max_redirects": 3},
            counters={"web_fetch_total": 1},
            recent_fetches=[],
        ),
        browser=BrowserState(
            service=connection,
            caps={"timeout_seconds": 10},
            counters={"browser_render_total": 1},
            recent_renders=[],
            recent_follows=[],
        ),
        counters={"status_queries": 1},
        recent_requests=[
            RecentRequest(
                timestamp="2026-03-19T22:00:00+00:00",
                event_type="status_query",
                request_id="req-1",
                trace_id="trace-1",
                actor="operator",
                source_service="bridge",
                outcome="success",
            )
        ],
        surfaces={"browser": "trusted_browser"},
        proposals=ProposalState(total=2, pending=1, approved=1),
    )


def make_proposals() -> list[ProposalRecord]:
    return [
        ProposalRecord(
            proposal_id="pending-1",
            action_type="http_post",
            action_payload={"url": "https://httpbin.org/post"},
            status="pending",
            created_by="agent",
            created_at="2026-03-19T22:01:00+00:00",
            request_id="req-pending",
            trace_id="trace-pending",
        ),
        ProposalRecord(
            proposal_id="executed-1",
            action_type="http_post",
            action_payload={"url": "https://httpbin.org/post"},
            status="executed",
            created_by="agent",
            created_at="2026-03-19T21:59:00+00:00",
            decided_by="operator",
            decided_at="2026-03-19T22:02:00+00:00",
            decision_reason="ok",
            executed_by="operator",
            executed_at="2026-03-19T22:03:00+00:00",
            execution_result={"http_status": 200},
            request_id="req-executed",
            trace_id="trace-executed",
        ),
    ]


@pytest.mark.fast
def test_home_renders_status_and_links(tmp_path: Path):
    settings = make_settings(tmp_path)
    write_demo_files(settings)
    app = create_app(
        settings=settings,
        bridge_api=FakeBridgeAPI(status=make_status(), proposals=make_proposals()),
        repo_data=RepoData(settings),
    )

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "stage8_consequential_actions" in response.text
    assert "Latest Run" in response.text
    assert "Latest Pending Proposal" in response.text


@pytest.mark.fast
def test_home_renders_bridge_degraded_state(tmp_path: Path):
    settings = make_settings(tmp_path)
    write_demo_files(settings)
    app = create_app(
        settings=settings,
        bridge_api=FakeBridgeAPI(error="bridge unavailable"),
        repo_data=RepoData(settings),
    )

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Bridge data unavailable." in response.text
    assert "bridge unavailable" in response.text


@pytest.mark.fast
def test_runs_page_renders_empty_and_non_empty_states(tmp_path: Path):
    settings = make_settings(tmp_path)
    app = create_app(
        settings=settings,
        bridge_api=FakeBridgeAPI(error="bridge unavailable"),
        repo_data=RepoData(settings),
    )

    with TestClient(app) as client:
        empty_response = client.get("/runs")

    assert "No run output JSON files exist yet" in empty_response.text

    write_demo_files(settings)
    app = create_app(
        settings=settings,
        bridge_api=FakeBridgeAPI(error="bridge unavailable"),
        repo_data=RepoData(settings),
    )
    with TestClient(app) as client:
        filled_response = client.get("/runs")

    assert "latest_seed_run.json" in filled_response.text
    assert "read site" in filled_response.text


@pytest.mark.fast
def test_run_detail_renders_steps_and_related_artifacts(tmp_path: Path):
    settings = make_settings(tmp_path)
    write_demo_files(settings)
    app = create_app(
        settings=settings,
        bridge_api=FakeBridgeAPI(error="bridge unavailable"),
        repo_data=RepoData(settings),
    )

    with TestClient(app) as client:
        response = client.get("/runs/latest_seed_run.json")

    assert response.status_code == 200
    assert "Run Summary" in response.text
    assert "bridge_status" in response.text
    assert "current_real_site_brief.md" in response.text


@pytest.mark.fast
def test_proposals_page_renders_status_filter(tmp_path: Path):
    settings = make_settings(tmp_path)
    app = create_app(
        settings=settings,
        bridge_api=FakeBridgeAPI(status=make_status(), proposals=make_proposals()),
        repo_data=RepoData(settings),
    )

    with TestClient(app) as client:
        response = client.get("/proposals?status=executed")

    assert response.status_code == 200
    assert "executed-1" in response.text
    assert "pending-1" not in response.text


@pytest.mark.fast
def test_proposal_detail_renders_execution_result(tmp_path: Path):
    settings = make_settings(tmp_path)
    app = create_app(
        settings=settings,
        bridge_api=FakeBridgeAPI(status=make_status(), proposals=make_proposals()),
        repo_data=RepoData(settings),
    )

    with TestClient(app) as client:
        response = client.get("/proposals/executed-1")

    assert response.status_code == 200
    assert "Execution Result" in response.text
    assert "http_status" in response.text


@pytest.mark.fast
def test_artifact_view_rejects_traversal_and_serves_allowed_files(tmp_path: Path):
    settings = make_settings(tmp_path)
    write_demo_files(settings)
    app = create_app(
        settings=settings,
        bridge_api=FakeBridgeAPI(error="bridge unavailable"),
        repo_data=RepoData(settings),
    )

    with TestClient(app) as client:
        reject_response = client.get("/artifacts/../secrets.txt")
        markdown_response = client.get("/artifacts/research/current_real_site_brief.md")
        image_response = client.get("/artifacts/research/current_real_site_screenshot.png")

    assert reject_response.status_code == 404
    assert markdown_response.status_code == 200
    assert "<article class=\"markdown\">" in markdown_response.text
    assert image_response.status_code == 200
    assert image_response.headers["content-type"] == "image/png"
