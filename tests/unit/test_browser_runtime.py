from fastapi.testclient import TestClient

from trusted.browser.app import app, browser_launch_args, browser_launch_kwargs


class DummyBrowser:
    async def close(self):
        return None


class DummyPlaywright:
    async def stop(self):
        return None


async def fake_launch_browser_runtime():
    return DummyPlaywright(), DummyBrowser()


def test_browser_launch_args_do_not_disable_sandbox():
    args = browser_launch_args()
    assert "--no-sandbox" not in args
    assert "--disable-setuid-sandbox" not in args
    assert browser_launch_kwargs()["chromium_sandbox"] is True


def test_browser_health_exposes_runtime_hardening_details(monkeypatch):
    monkeypatch.setattr("trusted.browser.app._launch_browser_runtime", fake_launch_browser_runtime)
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert "running_as_root" in body["details"]
    assert body["details"]["chromium_sandbox"] is True
    assert "launch_args" in body["details"]
    assert "--no-sandbox" not in body["details"]["launch_args"]
    assert "--disable-setuid-sandbox" not in body["details"]["launch_args"]
