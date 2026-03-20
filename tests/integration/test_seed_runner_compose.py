import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PROJECT = "rsi_stage3_seed_test"
LOG_PATH = ROOT / "runtime" / "trusted_state" / "logs" / "bridge_events.jsonl"
STATE_PATH = ROOT / "runtime" / "trusted_state" / "state" / "operational_state.json"
WORKSPACE_ROOT = ROOT / "untrusted" / "agent_workspace"
RUN_OUTPUTS = WORKSPACE_ROOT / "run_outputs"
BUDGET_CAP = 120

TEST_AGENT_TOKEN = "rsi-agent-token-dev-sentinel"
TEST_OPERATOR_TOKEN = "rsi-operator-token-dev-sentinel"


def agent_auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_AGENT_TOKEN}"}


def operator_auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_OPERATOR_TOKEN}"}


def docker_env() -> dict[str, str]:
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = COMPOSE_PROJECT
    env["RSI_LLM_BUDGET_TOKEN_CAP"] = str(BUDGET_CAP)
    env["RSI_WEB_ALLOWLIST_HOSTS"] = "allowed.test"
    env["RSI_FETCH_ALLOW_PRIVATE_TEST_HOSTS"] = "allowed.test"
    env["RSI_ACTION_ALLOWLIST_HOSTS"] = "allowed.test"
    return env


def run_command(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=check,
    )


def compose_command(
    args: list[str],
    *,
    env: dict[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return run_command(["docker", "compose", *args], env=env, check=check)


def compose_exec(
    service: str,
    command: list[str],
    *,
    env: dict[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return compose_command(["exec", "-T", service, *command], env=env, check=check)


def compose_http_response(
    service: str,
    method: str,
    url: str,
    *,
    env: dict[str, str],
    payload: dict | None = None,
    headers: dict | None = None,
) -> dict:
    code = (
        "import httpx, json\n"
        f"method = {method!r}\n"
        f"url = {url!r}\n"
        f"payload = {json.dumps(payload)!r}\n"
        f"headers = {json.dumps(headers or {})!r}\n"
        "with httpx.Client(timeout=15.0) as client:\n"
        "    response = client.request(method, url, json=json.loads(payload) if payload else None, headers=json.loads(headers))\n"
        "body = None\n"
        "try:\n"
        "    body = response.json()\n"
        "except Exception:\n"
        "    body = {'raw': response.text}\n"
        "print(json.dumps({'status_code': response.status_code, 'headers': dict(response.headers), 'json': body}))\n"
    )
    result = compose_exec(service, ["python", "-c", code], env=env)
    return json.loads(result.stdout)


def load_events() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    return [
        json.loads(line)
        for line in LOG_PATH.read_text(encoding="ascii").splitlines()
        if line.strip()
    ]


@pytest.fixture(scope="module")
def compose_stack():
    env = docker_env()
    docker_ready = run_command(["docker", "info"], env=env, check=False)
    if docker_ready.returncode != 0:
        pytest.fail("Docker daemon is required for Stage 3 seed runner tests")

    compose_command(["down", "--remove-orphans", "--volumes"], env=env, check=False)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOG_PATH.exists():
        LOG_PATH.unlink()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.exists():
        STATE_PATH.unlink()
    if RUN_OUTPUTS.exists():
        shutil.rmtree(RUN_OUTPUTS)

    compose_command(["up", "--build", "-d", "--wait"], env=env)
    yield env
    compose_command(["down", "--remove-orphans", "--volumes"], env=env, check=False)
    if RUN_OUTPUTS.exists():
        shutil.rmtree(RUN_OUTPUTS)


def test_seed_runner_uses_workspace_mount_and_bridge_surfaces(compose_stack):
    layout_probe = (
        "from pathlib import Path\n"
        "import json\n"
        "payload = {\n"
        "    'workspace_root': str(Path('/workspace/agent').resolve()),\n"
        "    'runtime_root': str(Path('/app/untrusted').resolve()),\n"
        "    'workspace_has_seedlib': Path('/workspace/agent/seedlib.py').exists(),\n"
        "    'runtime_has_seed_runner': Path('/app/untrusted/agent/seed_runner.py').exists(),\n"
        "}\n"
        "print(json.dumps(payload))\n"
    )
    probe = compose_exec("agent", ["python", "-c", layout_probe], env=compose_stack)
    probe_payload = json.loads(probe.stdout)
    assert probe_payload["workspace_root"] == "/workspace/agent"
    assert probe_payload["runtime_root"] == "/app/untrusted"
    assert probe_payload["workspace_has_seedlib"] is True
    assert probe_payload["runtime_has_seed_runner"] is True

    health_probe = (
        "import httpx, json\n"
        "r = httpx.get('http://127.0.0.1:8001/healthz', timeout=5.0)\n"
        "r.raise_for_status()\n"
        "print(json.dumps(r.json()))\n"
    )
    health = compose_exec("agent", ["python", "-c", health_probe], env=compose_stack)
    health_body = json.loads(health.stdout)
    assert health_body["details"]["workspace_writable"] is True
    assert health_body["details"]["runtime_code_writable"] is False

    result = compose_exec(
        "agent",
        [
            "python",
            "-m",
            "untrusted.agent.seed_runner",
            "--task",
            "write a local-only run report",
            "--planner",
            "scripted",
            "--script",
            ".seed_plans/stage3_local_task.json",
            "--max-steps",
            "8",
        ],
        env=compose_stack,
    )
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["finished_reason"] == "planner_finished"
    assert payload["steps_executed"] >= 4

    latest_summary = json.loads(
        (RUN_OUTPUTS / "latest_seed_run.json").read_text(encoding="ascii")
    )
    assert latest_summary["task"] == "write a local-only run report"
    assert any(step["kind"] == "bridge_status" for step in latest_summary["steps"])
    assert any(step["kind"] == "bridge_chat" for step in latest_summary["steps"])
    assert (RUN_OUTPUTS / "stage3_report.txt").exists()

    events = load_events()
    assert any(
        event["event_type"] == "agent_run"
        and event["summary"]["event_kind"] == "run_start"
        and event["summary"]["reported_origin"] == "untrusted_agent"
        for event in events
    )
    assert any(
        event["event_type"] == "agent_run"
        and event["summary"]["event_kind"] == "run_end"
        and event["summary"]["reported_origin"] == "untrusted_agent"
        for event in events
    )
    assert any(
        event["event_type"] == "status_query" and event["actor"] == "agent"
        for event in events
    )
    assert any(
        event["event_type"] == "llm_call" and event["actor"] == "agent"
        for event in events
    )


def test_seed_runner_can_render_fixture_and_create_pending_proposal(compose_stack):
    research_dir = WORKSPACE_ROOT / "research"
    if research_dir.exists():
        shutil.rmtree(research_dir)

    result = compose_exec(
        "agent",
        [
            "python",
            "-m",
            "untrusted.agent.seed_runner",
            "--task",
            "read one page and ask for approval to post a summary",
            "--planner",
            "scripted",
            "--script",
            ".seed_plans/stage8_real_site_approval_demo.json",
            "--input-url",
            "http://allowed.test/browser/rendered",
            "--proposal-target-url",
            "http://allowed.test/action/echo-post",
            "--max-steps",
            "8",
        ],
        env=compose_stack,
    )
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["finished_reason"] == "planner_finished"
    assert payload["proposal_target_url"] == "http://allowed.test/action/echo-post"

    brief = WORKSPACE_ROOT / "research" / "current_real_site_brief.md"
    screenshot = WORKSPACE_ROOT / "research" / "current_real_site_screenshot.png"
    approval = WORKSPACE_ROOT / "research" / "current_pending_approval.md"
    assert brief.exists()
    assert screenshot.exists()
    assert approval.exists()
    brief_text = brief.read_text(encoding="utf-8")
    assert "Source URL: http://allowed.test/browser/rendered" in brief_text
    assert "STAGE 6 FIXTURE" in brief_text
    assert "http://allowed.test/action/echo-post" in approval.read_text(encoding="utf-8")

    proposals = compose_http_response(
        "agent",
        "GET",
        "http://bridge:8000/proposals?status=pending",
        env=compose_stack,
        headers=agent_auth_headers(),
    )
    assert proposals["status_code"] == 200
    matching = [
        proposal
        for proposal in proposals["json"]["proposals"]
        if proposal["action_type"] == "http_post"
        and proposal["action_payload"]["url"] == "http://allowed.test/action/echo-post"
    ]
    assert matching
    proposal = matching[-1]
    assert proposal["created_by"] == "agent"
    assert proposal["status"] == "pending"

    events = load_events()
    assert any(
        event["event_type"] == "proposal_created"
        and event["actor"] == "agent"
        and event["summary"]["action_type"] == "http_post"
        for event in events
    )
