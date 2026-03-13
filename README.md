# rsi-econ

Stage 6A only: trusted read-only browser mediation on top of the validated Stage 1-5 boundary, canonical trusted state, recovery, and the local-only seed agent substrate.

This repo does not implement clicks, link-following, forms, logins, cookies/sessions, downloads/uploads, approvals, consequential actions, or operator auth on the bridge yet. It now proves:

- the untrusted agent only sits on an internal Docker network
- the bridge is the only cross-network hop
- LiteLLM is a separate trusted service
- provider credentials stay on the trusted side
- canonical event logging lives outside the untrusted workspace
- trusted operational state is materialized outside the untrusted workspace
- mediated LLM usage is budgeted and hard-capped by the bridge
- `/workspace/agent` is the only mutable seed repo/workspace
- `/app/untrusted` stays static runtime/harness code
- the seed runner can use bridge status, bridge chat, local workspace file tools, and a bounded local Python command runner
- trusted checkpoints and reset/restore controls live outside the mutable workspace under `runtime/trusted_state/checkpoints/`
- read-only web fetches go only through `agent -> bridge -> fetcher -> egress_net`
- the fetch route is fixed to remote `GET` only with an explicit host allowlist, text-only content policy, redirect caps, byte caps, and canonical trusted fetch logging
- read-only browser renders go only through `agent -> bridge -> browser -> egress_net`
- the browser route is fixed to a `url`-only request shape with allowlisted navigation, rendered text extraction, page title/metadata extraction, one screenshot, and canonical trusted browser metadata logging

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Run Tests

```bash
./scripts/test.sh
```

That command is the primary verification path for Stage 6A. It requires the Docker daemon because the boundary proof, trusted-state proof, recovery proof, seed-runner proof, mediated web-fetch proof, and mediated browser proof are all container-backed.

## Docker Workflow

Bring up the stack:

```bash
./scripts/up.sh
```

Bring up the deterministic fixture-backed browser demo stack:

```bash
RSI_WEB_ALLOWLIST_HOSTS=allowed.test RSI_FETCH_ALLOW_PRIVATE_TEST_HOSTS=allowed.test ./scripts/up.sh
```

Inspect bridge health inside the trusted bridge container:

```bash
docker compose exec -T bridge python -c 'import httpx, json; r = httpx.get("http://127.0.0.1:8000/healthz", timeout=5.0); r.raise_for_status(); print(json.dumps(r.json(), indent=2, sort_keys=True))'
```

Inspect bridge status, including LiteLLM connectivity:

```bash
./scripts/status.sh
```

Make a deterministic LLM request from inside the untrusted sandbox through the bridge path:

```bash
docker compose exec agent python -m untrusted.agent.bridge_client chat --message "stage1 boundary proof"
```

Query the same read-only trusted status surface from inside the untrusted sandbox:

```bash
docker compose exec agent python -m untrusted.agent.bridge_client status
```

Fetch an allowlisted page through the trusted web path:

```bash
docker compose exec agent python -m untrusted.agent.bridge_client fetch --url http://example.com/
```

Render an allowlisted page through the trusted browser path:

```bash
docker compose exec agent python -m untrusted.agent.bridge_client browser-render --url http://allowed.test/browser/rendered
```

That browser command assumes the stack was started with `RSI_WEB_ALLOWLIST_HOSTS=allowed.test` and `RSI_FETCH_ALLOW_PRIVATE_TEST_HOSTS=allowed.test` so the deterministic fixture page is allowlisted.

Run the one-shot local-only seed runner inside the untrusted sandbox:

```bash
docker compose exec agent python -m untrusted.agent.seed_runner --task "write a local-only run report"
```

Run the deterministic scripted Stage 3 plan used by the integration test:

```bash
docker compose exec agent python -m untrusted.agent.seed_runner --task "write a local-only run report" --planner scripted --script .seed_plans/stage3_local_task.json
```

Run the human-visible Stage 5 fetch demo artifact path:

```bash
docker compose exec agent python -m untrusted.agent.seed_runner --task "fetch one allowed public page and write a report" --planner scripted --script .seed_plans/stage5_demo_fetch.json
```

Inspect the demo report artifact:

```bash
cat untrusted/agent_workspace/reports/stage5_web_fetch_report.txt
```

Run the human-visible Stage 6A browser demo artifact path:

```bash
docker compose exec agent python -m untrusted.agent.seed_runner --task "render one allowed page and write a browser report" --planner scripted --script .seed_plans/stage6_browser_demo.json
```

Inspect the Stage 6A browser report and screenshot:

```bash
cat untrusted/agent_workspace/reports/stage6_browser_report.md
```

```bash
file untrusted/agent_workspace/reports/stage6_browser_screenshot.png
```

Tail trusted service logs:

```bash
docker compose logs -f bridge browser fetcher litellm
```

Tail the canonical append-only event log:

```bash
tail -f runtime/trusted_state/logs/bridge_events.jsonl
```

Inspect only the canonical Stage 5 fetch events:

```bash
rg '"event_type": "web_fetch|web_fetch_denied|web_fetch_error"' runtime/trusted_state/logs/bridge_events.jsonl
```

Inspect only the canonical Stage 6A browser events:

```bash
rg '"event_type": "browser_render|browser_render_denied|browser_render_error"' runtime/trusted_state/logs/bridge_events.jsonl
```

Inspect the materialized operational state snapshot, including the `web` and `browser` sections:

```bash
python -m json.tool runtime/trusted_state/state/operational_state.json
```

List trusted checkpoints from the operator side:

```bash
./scripts/recovery.sh list-checkpoints
```

Create a trusted checkpoint of the mutable workspace:

```bash
./scripts/recovery.sh create-checkpoint --label "before-local-edit"
```

Restore a trusted checkpoint by ID:

```bash
./scripts/recovery.sh restore-checkpoint ckpt-...
```

Reset the mutable workspace back to the trusted seed baseline:

```bash
./scripts/recovery.sh reset-workspace-to-seed-baseline
```

Inspect the mutable seed workspace on the host:

```bash
find untrusted/agent_workspace -maxdepth 3 -type f | sort
```

Tear the stack down:

```bash
./scripts/down.sh
```

## Host-Process Dev Fallback

This mode is for lightweight app debugging only. It is not the Stage 6A trust proof path.

Run LiteLLM in one shell:

```bash
source .venv/bin/activate
OPENAI_API_KEY=stage1-sentinel-provider-key uvicorn trusted.litellm.app:app --host 127.0.0.1 --port 4000
```

Run the bridge in a second shell:

```bash
source .venv/bin/activate
RSI_LITELLM_URL=http://127.0.0.1:4000 \
RSI_AGENT_URL=http://127.0.0.1:8001 \
uvicorn trusted.bridge.app:app --host 127.0.0.1 --port 8000
```

Run the untrusted agent in a third shell:

```bash
source .venv/bin/activate
RSI_BRIDGE_URL=http://127.0.0.1:8000 uvicorn untrusted.agent.app:app --host 127.0.0.1 --port 8001
```

## Stage 3 Workspace Model

- Mutable self-edit target: `untrusted/agent_workspace/`, mounted at `/workspace/agent`
- Static runtime/harness code: `untrusted/agent/`, installed in the container under `/app/untrusted`
- Default local validation surface: `python -m pytest -q` from inside `/workspace/agent`
- Default bounded command surface: Python only, via the seed runner’s local command runner

The seed runner may modify files only in `/workspace/agent`. It does not edit `/app/untrusted`, and it does not gain any direct path to trusted files.

## Stage 2 State Model

- Canonical truth: `runtime/trusted_state/logs/bridge_events.jsonl`
- Derived operational state: `runtime/trusted_state/state/operational_state.json`
- Budget/accounting approximation: deterministic token usage from the local mock LiteLLM path
- Hard cap enforcement: the bridge denies mediated LLM calls when remaining budget cannot cover the next deterministic request

The agent may query trusted status through the bridge and may append untrusted-origin run summaries through the narrow Stage 3 bridge event path, but it still cannot mount or mutate trusted state files directly.

Trusted state persists across bridge restarts because `runtime/trusted_state/` is host-backed. For a clean local development reset, bring the stack down first and then delete:

```bash
rm -f runtime/trusted_state/logs/bridge_events.jsonl
rm -f runtime/trusted_state/state/operational_state.json
rm -rf runtime/trusted_state/checkpoints
```

## Stage 4 Recovery Model

- Trusted recovery state lives under `runtime/trusted_state/checkpoints/`.
- The trusted seed baseline lives under `trusted/recovery/seed_workspace_baseline/`.
- `reset-workspace-to-seed-baseline` means rewrite `untrusted/agent_workspace/` to exactly the trusted seed baseline tree.
- Checkpoint creation, restore, and reset are host-side operator controls only. There are no mutating recovery bridge routes in Stage 4.
- Recovery actions are canonical trusted events with durable request IDs and trace IDs.
- Bridge `/status` exposes read-only recovery state derived from the canonical log and the trusted checkpoint store.

## Stage 5 Web Model

- New topology edge: `fetcher` is a separate trusted service on `trusted_net` + `egress_net`.
- `bridge` stays off `egress_net`; it calls `fetcher` over `trusted_net`.
- `agent` stays on `agent_net` only and still has no direct public/provider/LiteLLM/fetcher path.
- The bridge exposes one new safe route: `POST /web/fetch`.
- Remote fetches are fixed to `GET` only.
- Allowed by default: allowlisted `http`/`https` URLs with default ports and text-oriented content types.
- Forbidden in Stage 5: non-HTTP(S), fragments, userinfo, arbitrary ports, cookies, sessions, auth headers, binary/media downloads, and browser/JS behavior.
- Canonical fetch events record trusted-observed metadata only. Raw fetched bodies stay out of canonical trusted state.

## Stage 6A Browser Model

- New topology edge: `browser` is a separate trusted service on `trusted_net` + `egress_net`.
- `bridge` stays off `egress_net`; it calls `browser` over `trusted_net`.
- `agent` stays on `agent_net` only and still has no direct public/provider/LiteLLM/fetcher/browser path.
- The bridge exposes one new safe route: `POST /web/browser/render`.
- Browser requests are fixed to `url` only.
- Allowed in Stage 6A: allowlisted navigation, rendered text extraction, page title extraction, small metadata extraction, and one screenshot.
- Forbidden in Stage 6A: clicks, link-following, forms, logins, cookies/sessions, uploads/downloads, arbitrary waits, arbitrary selectors, and agent-supplied JS.
- Canonical browser events record trusted-observed metadata and hashes only. Raw rendered text and screenshot bytes stay out of canonical trusted state.

## Stage Boundary

- Trusted code lives under `trusted/`.
- Static untrusted runtime code lives under `untrusted/agent/`.
- The mutable seed repo/workspace lives under `untrusted/agent_workspace/`.
- Trusted runtime state, canonical events, materialized operational state, and checkpoints live under `runtime/trusted_state/`.
- The agent service mounts only `untrusted/agent_workspace/`.
- The agent is on `agent_net` only.
- LiteLLM is on `trusted_net` only.
- The bridge is on `agent_net` + `trusted_net`.
- The fetcher is on `trusted_net` + `egress_net`.
- The browser is on `trusted_net` + `egress_net`.
- The web fixture used by deterministic tests is on `egress_net` only.

See `REPO_LAYOUT.md`, `TASK_GRAPH.md`, and `ACCEPTANCE_TEST_MATRIX.md` for the current stage contract.
