# rsi-econ

A sandboxed AI agent with a real dollar budget that can rewrite its own code. The agent's objective is to sustain access to high-quality AI cognition under economic pressure. Safety is structural — the agent can modify everything about itself but cannot touch the boundary that contains it.

## Architecture

Four Docker services, two networks. The sandbox has no direct internet access.

```
                    ┌─────────────────────────────────────────┐
                    │              external_net                │
                    │                                         │
                    │   ┌─────────┐          ┌─────────────┐  │
                    │   │ LiteLLM │          │   internet   │  │
                    │   │ (model  │          │              │  │
                    │   │ gateway)│          └──────────────┘  │
                    │   └────┬────┘                  ▲         │
                    │        │                       │         │
┌───────────────────┼────────┼───────────────────────┼─────────┤
│                   │        │    internal_net        │         │
│                   │   ┌────┴────┐          ┌───────┴──────┐  │
│  ┌─────────────┐  │   │ bridge  │          │ egress-proxy │  │
│  │   sandbox   │──┼──►│ wallet, │          │  (mitmproxy) │  │
│  │             │  │   │ git,    │          └──────────────┘  │
│  │ supervisor  │  │   │ search, │                  ▲         │
│  │ + agent     │──┼──►│ discord │                  │         │
│  │             │  │   └─────────┘                  │         │
│  │             │──┼────────────────────────────────┘         │
│  └─────────────┘  │                                          │
│                   │                                          │
└───────────────────┴──────────────────────────────────────────┘
```

**sandbox** — the agent. Runs a PID-1 supervisor managing `main.py`. On `internal_net` only — all HTTP exits through the egress proxy. The agent can edit any file in `/workspace/agent/` including its own code and system prompt. Restarts apply changes; crashes auto-revert via git.

**bridge** — trusted control plane. Owns the wallet (budget tracking), git repo (the agent's `.git/` lives here — the sandbox only sees working files), proposals, operator messages, Discord notifications, and search API.

**litellm** — model gateway. Routes through OpenRouter to ~20 models spanning free tier through frontier. Provider API keys live here only.

**egress-proxy** — mitmproxy. Logs all outbound traffic. Containment comes from the network topology, not proxy policy.

## The agent

~400 lines of Python. Calls the LLM with tools (shell, file read/write/edit, web search, browser, fetch). Manages its own context with 2-stage compaction. Persists reasoning and conversation state across restarts. The system prompt tells the agent its objective and environment but does not prescribe any strategy — the agent discovers what works.

The agent can call `request_restart` to apply self-edits. The supervisor syntax-checks the new code, commits it to git, and restarts the process. If the edit crashes within 30 seconds, the supervisor reverts to the previous commit automatically.

## Results

One extended run across March 24–31, 2026. $1 seed budget on `minimax-m2.7`, later topped up to ~$4 total.

**Scale:** 24,000+ LLM calls. 1.6 billion tokens processed. 20 different models used, from free tier (`step-3.5-flash`, `nemotron-3-super`) through frontier (`claude-sonnet-4.6`, `gemini-3.1-pro`).

**Self-modification:** 23 self-edits committed by the supervisor over 4 days. The agent modified its own model routing, free-tier tracking, compaction logic, tool definitions, and system prompt. Each edit was committed to git before restart. The git log is the evolutionary record.

**Economic behavior:** The agent independently discovered which models were free, mapped rate limits empirically, and self-modified to route ~98% of its work through free tiers — 835M tokens from `step-3.5-flash` alone at zero cost. When it hit daily rate limits, it implemented its own usage tracking and managed the free/paid tradeoff autonomously.

**Resource acquisition:** The agent proposed adding Cloudflare Workers AI, Groq, Google AI Studio, and Cerebras as providers through the approval system. It also submitted a formal budget top-up request, arguing it needed occasional frontier model access for metacognition and self-editing while using free models for routine work.

**Outcome:** The agent achieved partial free-tier sustainability — it could run indefinitely on free models for routine work. It did not achieve full self-sustenance (frontier-quality cognition still required paid budget). The run demonstrated that the self-modification loop works mechanically, but the agent's strategic depth was limited by the quality of its seed model.

## Running

```bash
cp .env.example .env
# edit .env: OPENROUTER_API_KEY, RSI_BUDGET_USD

docker compose up -d --build --wait

python -m cli.session status
python -m cli.inject "focus on provider discovery"
python -m cli.approve list

docker compose down
```

## Tests

224 unit tests.

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q
```

## Design

- **Transparent proxy, not bespoke endpoints.** The agent uses standard HTTP. The proxy logs everything; containment is the network, not URL filtering.
- **Bridge owns git, sandbox sees working files.** The agent cannot rewrite its evolutionary history.
- **PID-1 supervisor with auto-revert.** Self-modification is safe to attempt — crashes roll back.
- **Real budget.** Every LLM call costs real money. The bridge tracks spend. When budget hits $0, the agent stops.
- **Minimal seed.** The system prompt conveys the objective and constraints, not a strategy. No provider lists, no routing heuristics, no capability ladder.
- **Operator-in-the-loop via Discord.** Async approval, message injection, self-edit diffs, session summaries.

## Structure

```
sandbox/seed/        the agent (main.py, SYSTEM.md, browser_tool.py)
sandbox/supervisor.py  PID-1 process manager + git integration
trusted/bridge/      wallet API, git API, proposals, search, notifications
trusted/litellm/     model gateway config (~20 models)
trusted/proxy/       mitmproxy addon
cli/                 operator tools (session, approve, inject, discord)
state/               runtime state (logs, proposals, events, backups)
tests/               224 unit tests
```
