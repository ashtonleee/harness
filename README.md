# rsi-econ

Stage 3 only: minimal local-only seed agent substrate on top of the validated Stage 1/2 boundary and canonical trusted state.

This repo does not implement public-web proxying, browser automation, approvals, recovery, or consequential actions yet. It now proves the boundary, the trusted-side control plane, and a minimal local-only seed-agent loop:

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

That command is the primary verification path for Stage 3. It requires the Docker daemon because the boundary proof, trusted-state proof, and seed-runner proof are all container-backed.

## Docker Workflow

Bring up the stack:

```bash
./scripts/up.sh
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

Run the one-shot local-only seed runner inside the untrusted sandbox:

```bash
docker compose exec agent python -m untrusted.agent.seed_runner --task "write a local-only run report"
```

Run the deterministic scripted Stage 3 plan used by the integration test:

```bash
docker compose exec agent python -m untrusted.agent.seed_runner --task "write a local-only run report" --planner scripted --script .seed_plans/stage3_local_task.json
```

Tail trusted service logs:

```bash
docker compose logs -f bridge litellm
```

Tail the canonical append-only event log:

```bash
tail -f runtime/trusted_state/logs/bridge_events.jsonl
```

Inspect the materialized operational state snapshot:

```bash
python -m json.tool runtime/trusted_state/state/operational_state.json
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

This mode is for lightweight app debugging only. It is not the Stage 3 trust proof path.

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
```

## Stage Boundary

- Trusted code lives under `trusted/`.
- Static untrusted runtime code lives under `untrusted/agent/`.
- The mutable seed repo/workspace lives under `untrusted/agent_workspace/`.
- Trusted runtime state, canonical events, and materialized operational state live under `runtime/trusted_state/`.
- The agent service mounts only `untrusted/agent_workspace/`.
- The agent is on `agent_net` only.
- LiteLLM is on `trusted_net` only.
- The bridge is the only service on both networks.

See `REPO_LAYOUT.md`, `TASK_GRAPH.md`, and `ACCEPTANCE_TEST_MATRIX.md` for the current stage contract.
