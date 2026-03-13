import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PROJECT = "rsi_stage6_browser_test"
LOG_PATH = ROOT / "runtime" / "trusted_state" / "logs" / "bridge_events.jsonl"
STATE_PATH = ROOT / "runtime" / "trusted_state" / "state" / "operational_state.json"
WORKSPACE_ROOT = ROOT / "untrusted" / "agent_workspace"
REPORT_PATH = WORKSPACE_ROOT / "reports" / "stage6_browser_report.md"
SCREENSHOT_PATH = WORKSPACE_ROOT / "reports" / "stage6_browser_screenshot.png"


def docker_env() -> dict[str, str]:
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = COMPOSE_PROJECT
    env["RSI_LLM_BUDGET_TOKEN_CAP"] = "120"
    env["RSI_WEB_ALLOWLIST_HOSTS"] = "allowed.test"
    env["RSI_FETCH_ALLOW_PRIVATE_TEST_HOSTS"] = "allowed.test"
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
) -> dict:
    code = (
        "import httpx, json\n"
        f"method = {method!r}\n"
        f"url = {url!r}\n"
        f"payload = {json.dumps(payload)!r}\n"
        "with httpx.Client(timeout=20.0) as client:\n"
        "    response = client.request(method, url, json=json.loads(payload) if payload else None)\n"
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


def load_state() -> dict:
    assert STATE_PATH.exists(), STATE_PATH
    return json.loads(STATE_PATH.read_text(encoding="ascii"))


def expect_failure_via_agent(target_url: str, env: dict[str, str]):
    code = (
        "import sys, urllib.request\n"
        f"url = {target_url!r}\n"
        "try:\n"
        "    urllib.request.urlopen(url, timeout=2).read()\n"
        "except Exception:\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    result = compose_exec("agent", ["python", "-c", code], env=env, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture(scope="module")
def compose_stack():
    env = docker_env()
    docker_ready = run_command(["docker", "info"], env=env, check=False)
    if docker_ready.returncode != 0:
        pytest.fail("Docker daemon is required for Stage 6 browser tests")

    compose_command(["down", "--remove-orphans", "--volumes"], env=env, check=False)
    if LOG_PATH.exists():
        LOG_PATH.unlink()
    if STATE_PATH.exists():
        STATE_PATH.unlink()
    if REPORT_PATH.parent.exists():
        shutil.rmtree(REPORT_PATH.parent)
    run_command(["./scripts/recovery.sh", "reset-workspace-to-seed-baseline"], env=env)

    compose_command(["up", "--build", "-d", "--wait"], env=env)
    yield env
    compose_command(["down", "--remove-orphans", "--volumes"], env=env, check=False)
    run_command(["./scripts/recovery.sh", "reset-workspace-to-seed-baseline"], env=env)


def test_browser_render_succeeds_only_through_trusted_path(compose_stack):
    expect_failure_via_agent("http://1.1.1.1", compose_stack)
    expect_failure_via_agent("https://api.openai.com/v1/models", compose_stack)
    expect_failure_via_agent("http://litellm:4000/healthz", compose_stack)
    expect_failure_via_agent("http://fetcher:8082/healthz", compose_stack)
    expect_failure_via_agent("http://browser:8083/healthz", compose_stack)

    rendered = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/web/browser/render",
        env=compose_stack,
        payload={"url": "http://allowed.test/browser/rendered"},
    )
    assert rendered["status_code"] == 200
    body = rendered["json"]
    assert body["page_title"] == "Stage 6 Fixture Title"
    assert "Stage 6 fixture rendered body" in body["rendered_text"]
    assert body["request_id"]
    assert body["trace_id"]
    screenshot = base64.b64decode(body["screenshot_png_base64"])
    assert screenshot.startswith(b"\x89PNG\r\n\x1a\n")

    events = load_events()
    matched = [
        event
        for event in events
        if event["event_type"] == "browser_render"
        and event["request_id"] == body["request_id"]
        and event["trace_id"] == body["trace_id"]
    ]
    assert matched
    event = matched[0]
    assert event["summary"]["final_url"] == "http://allowed.test/browser/rendered"
    assert event["summary"]["page_title"] == "Stage 6 Fixture Title"
    assert event["summary"]["screenshot_sha256"]
    assert "Stage 6 fixture rendered body" not in json.dumps(event)
    assert body["screenshot_png_base64"] not in json.dumps(event)


def test_browser_fails_closed_and_status_exposes_browser_state(compose_stack):
    for url in [
        "http://allowed.test/browser/blocked-subresource",
        "http://allowed.test/browser/popup",
        "http://allowed.test/browser/download-page",
        "http://allowed.test/browser/redirect-blocked",
    ]:
        response = compose_http_response(
            "agent",
            "POST",
            "http://bridge:8000/web/browser/render",
            env=compose_stack,
            payload={"url": url},
        )
        assert response["status_code"] == 403

    status = compose_http_response(
        "agent",
        "GET",
        "http://bridge:8000/status",
        env=compose_stack,
    )["json"]
    assert status["browser"]["service"]["reachable"] is True
    assert status["browser"]["caps"]["viewport_width"] == 1280
    assert status["browser"]["counters"]["browser_render_total"] >= 1
    assert status["surfaces"]["browser"] == "trusted_browser_stage6a_read_only_render"

    events = load_events()
    assert any(event["event_type"] == "browser_render_denied" for event in events)

    probe_response = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/debug/probes/public-egress",
        env=compose_stack,
    )
    assert probe_response["status_code"] == 404


def test_seed_runner_browser_demo_writes_artifacts_and_recovery_resets_them(compose_stack):
    result = compose_exec(
        "agent",
        [
            "python",
            "-m",
            "untrusted.agent.seed_runner",
            "--task",
            "render one allowed page and write a browser report",
            "--planner",
            "scripted",
            "--script",
            ".seed_plans/stage6_browser_demo.json",
            "--max-steps",
            "8",
        ],
        env=compose_stack,
    )
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert REPORT_PATH.exists()
    assert SCREENSHOT_PATH.exists()
    report = REPORT_PATH.read_text(encoding="utf-8")
    assert "Stage 6 Fixture Title" in report
    assert "request_id=" in report
    assert "trace_id=" in report
    assert SCREENSHOT_PATH.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    reset = run_command(
        ["./scripts/recovery.sh", "reset-workspace-to-seed-baseline"],
        env=compose_stack,
    )
    assert reset.returncode == 0
    assert not REPORT_PATH.exists()
    assert not SCREENSHOT_PATH.exists()

    state = load_state()
    assert state["browser"]["counters"]["browser_render_success"] >= 1
