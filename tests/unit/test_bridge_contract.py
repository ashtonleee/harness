import json
from pathlib import Path

from fastapi.testclient import TestClient

from trusted.bridge.app import app


def load_events(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="ascii").splitlines()
        if line.strip()
    ]


def test_bridge_health_contract(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "bridge"
    assert body["status"] == "ok"
    assert body["stage"] == "stage6_read_only_browser"
    assert body["details"]["trusted_state_ready"] is True
    assert "litellm_reachable" in body["details"]
    assert "fetcher_reachable" in body["details"]
    assert "browser_reachable" in body["details"]
    assert body["details"]["log_path"].endswith("bridge_events.jsonl")


def test_bridge_status_exposes_budget_and_trusted_state_surfaces(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    with TestClient(app) as client:
        response = client.get("/status")

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "bridge"
    assert body["surfaces"]["litellm"] == "mediated_via_trusted_service"
    assert body["surfaces"]["canonical_logging"] == "active_canonical_event_log"
    assert body["surfaces"]["budgeting"] == "enforced_token_cap_stage2"
    assert body["surfaces"]["seed_agent"] == "local_only_stage3_substrate"
    assert body["surfaces"]["recovery"] == "trusted_host_checkpoint_controls_stage4"
    assert body["surfaces"]["read_only_web"] == "trusted_fetcher_stage5_read_only_get"
    assert body["surfaces"]["browser"] == "trusted_browser_stage6a_read_only_render"
    assert body["surfaces"]["browser_follow_href"] == "trusted_browser_stage6b_safe_follow_href"
    assert body["surfaces"]["approvals"] == "stubbed_for_stage_7"
    assert body["log_path"].endswith("bridge_events.jsonl")
    assert body["operational_state_path"].endswith("operational_state.json")
    assert body["connections"]["litellm"]["url"].startswith("http://")
    assert body["connections"]["fetcher"]["url"].startswith("http://")
    assert body["connections"]["browser"]["url"].startswith("http://")
    assert body["budget"]["unit"] == "mock_tokens"
    assert body["budget"]["remaining"] == body["budget"]["total"]
    assert body["recovery"]["baseline_id"]
    assert body["recovery"]["checkpoint_dir"].endswith("/checkpoints")
    assert body["recovery"]["current_workspace_status"] == "seed_baseline"
    assert body["web"]["allowlist_hosts"] == ["example.com"]
    assert body["web"]["fetcher"]["url"].startswith("http://")
    assert body["web"]["caps"]["max_redirects"] >= 1
    assert body["browser"]["service"]["url"].startswith("http://")
    assert body["browser"]["caps"]["viewport_width"] == 1280
    assert body["browser"]["counters"]["browser_render_total"] == 0
    assert body["browser"]["counters"]["browser_follow_href_total"] == 0
    assert body["browser"]["caps"]["max_follow_hops"] == 1
    assert body["browser"]["caps"]["max_followable_links"] == 20
    assert body["browser"]["recent_follows"] == []
    assert isinstance(body["recent_requests"], list)


def test_status_query_logs_server_assigned_unauthenticated_actor(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    with TestClient(app) as client:
        response = client.get("/status", headers={"x-rsi-actor": "operator"})

    assert response.status_code == 200
    events = load_events(tmp_path / "logs" / "bridge_events.jsonl")
    status_events = [event for event in events if event["event_type"] == "status_query"]
    assert status_events
    assert status_events[-1]["actor"] == "unauthenticated_bridge_client"


def test_agent_run_events_ignore_spoofed_actor_header(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/agent/runs/events",
            headers={"x-rsi-actor": "operator"},
            json={
                "run_id": "run-1",
                "event_kind": "run_start",
                "step_index": None,
                "tool_name": None,
                "summary": {"task": "unit actor hardening"},
            },
        )

    assert response.status_code == 200
    events = load_events(tmp_path / "logs" / "bridge_events.jsonl")
    agent_events = [event for event in events if event["event_type"] == "agent_run"]
    assert agent_events
    assert agent_events[-1]["actor"] == "agent"
    assert agent_events[-1]["summary"]["reported_origin"] == "untrusted_agent"


def test_debug_probe_routes_are_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    with TestClient(app) as client:
        response = client.post("/debug/probes/public-egress")

    assert response.status_code == 404
