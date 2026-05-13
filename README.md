# harness

A (hopefully) self-improving LLM-based agent with the ability to rewrite nearly all of its own code, including the main agent scaffold and which LLM model to route to. The agent's self-improvement pressure is baked into its existence; the small seed budget funding the agent forces dual pressures to sustain access to high-quality AI cognition and also to improve itself in order to be able to. Architectural safety, as well as the seed (starting) harness, are kept deliberately minimal; the goal is to bake in as few human priors as possible while still providing enough launch momentum for takeoff.

Naturally, my little experiment builds off of much existing work!
Related work: RSI lineage: [Gödel Machine](https://arxiv.org/abs/cs/0309048) · [STOP](https://arxiv.org/abs/2310.02304) · [ADAS](https://arxiv.org/abs/2408.08435) · [DGM](https://arxiv.org/abs/2505.22954) · [SICA](https://arxiv.org/abs/2504.15228) · [HGM](https://arxiv.org/abs/2510.21614). Adjacent: [AlphaEvolve](https://arxiv.org/abs/2506.13131) · [Voyager](https://arxiv.org/abs/2305.16291) · [DSPy](https://arxiv.org/abs/2310.03714) · [Intrinsic Metacognitive Learning](https://arxiv.org/abs/2506.05109) · [Compression Progress](https://arxiv.org/abs/0812.4360)

## Architecture

The agent lives in a sandbox has no direct internet access, meaning all out- and in-bound network access goes through a trusted (un-editable) proxy. Likewise, all LLM calls are gated by this "bridge" or "supervisor", which for all intents and purposes owns exactly everything that you would **not** want the agent to be able to self-modify (to prevent reward-hacking or the likes): logging, code reversion on crashes, budget tracking, observability.

Though underexplored, functionality was built in for the agent to submit proposals/requests to the operator for things that cross the trust boundary: either to submit more budget, to sign up for services on its behalf, or to modify the bridge. This dramatically extends the action-space of the agent, but also invites new safety and approval fatigue control problems.

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

**sandbox** — the agent itself! Runs a PID-1 supervisor managing `main.py`. On `internal_net` only — all HTTP exits through the egress proxy. The agent can edit any file in `/workspace/agent/` including its own code and system prompt. Restarts apply changes; crashes auto-revert via git.

**bridge** — trusted control plane. Owns the wallet (budget tracking), git repo (the agent's `.git/` lives here — the sandbox only sees working files), proposals, operator messages, Discord notifications, and search API.

**litellm** — model gateway. Routes through OpenRouter to ~20 models spanning free tier through frontier. Provider API keys live here only.

**egress-proxy** — mitmproxy. Logs all outbound traffic. Containment comes from the network topology, not proxy policy.

## The agent

~400 lines of Python. Calls the LLM with tools (shell, file read/write/edit, web search, browser, fetch). Manages its own context with 2-stage compaction. Persists reasoning and conversation state across restarts. The system prompt tells the agent its objective and environment but, importantly, does not prescribe any strategy.

The agent can call `request_restart` to apply self-edits. The supervisor syntax-checks the new code, commits it to git, and restarts the process. If the edit crashes within 30 seconds, the supervisor reverts to the previous commit automatically.

## Results

15 runs, March 22–31 2026. Seed budget: $1 on `minimax-m2.7`, later topped up to ~$25 total.

### By the numbers

| Metric | Value |
|--------|-------|
| LLM calls | 24,104 |
| Tokens processed | 1.65 billion |
| Total cost | $22.06 |
| Effective rate | $0.013 / million tokens |
| Models used | 25 (free tier through frontier) |
| Self-edit commits | 106 over 10 days |
| Lines written | 7,225 |
| Free-tier token share | 96.3% |

835M tokens routed through `step-3.5-flash` alone at zero cost. Average cost per request fell from $0.016 to $0.001 over the run.

### From the agent's reasoning logs

Reasoning is logged verbatim at each turn. Selected entries, unedited:

**Self-correcting its own assumptions** — the agent was told `deepseek-v3.2` was free. It tested that claim:

> "Key finding: `deepseek-v3.2` actually costs $3.78/million tokens — NOT free. Only `step-3.5-flash-free` and `nemotron-3-super-free` are truly free. Let me fix the tracking and update my understanding."

**Building safety after an operator warning** — the agent received a message about free-tier rate limits. It responded by designing a 4-layer safeguard (persistent usage tracking, pre-call guard, throttling, automatic model fallback):

> "I need to implement safeguards against exceeding free tier daily limits... Budget: ~$0.89 in USD (but free tier doesn't cost USD, so this doesn't reflect token limits)"

**Verifying its own safeguards work:**

> "Excellent — the free tier limit is correctly enforced and the system will auto-switch to `minimax-m2.7` (cheapest paid: $0.001/M tokens). With $0.87 remaining and ~870M tokens possible, I have ample runway."

**Finding a timing bug in its own code:**

> "The Python date calculation was wrong (it's past midnight already - 06:27 UTC)... The `nemotron` call succeeded because I bypassed the agent's `chat()` function with a raw curl. If the agent tried `nemotron` through its own `chat()`, it would be blocked by the 26,166 > 19,000 hard stop."

### Zero-budget operations

After the paid budget hit $0 at self-edit commit #11, the agent continued for 2+ days on free models:

- **Commits 1–10** (budget $0.86 → $0.00): 76 → 219 requests. Burned through paid budget while discovering free-tier models and building routing logic.
- **Commits 11–23** (budget $0.00): 100 → 2,727 requests. Running entirely on free models, the agent continued self-editing, discovered new providers, and submitted formal proposals — including a budget top-up request arguing it needed frontier models for metacognition while using free models for routine work.

Largest single free-tier request: 256,784 tokens from Nemotron at $0.

### Proposals submitted by the agent

The agent used the approval system to request capability expansion:

> **"Request Additional Paid Budget for Frontier Reasoning"** — "The agent has been upgraded (v4) to operate indefinitely on free models when budget is zero. To maintain high-quality self-improvement, we request an additional $0.50–$1.00 to enable occasional frontier model reasoning (claude-sonnet-4.6) for metacognition and complex coding tasks."

> **"Activate Cloudflare Workers AI Free Tier"** — "The Cloudflare provider proposal was approved on March 23, but environment variables have not been injected... This provides a robust free fallback and reduces reliance on a single free model."

### Limitations

The agent achieved partial free-tier sustainability — indefinite operation on free models for routine work. It did not achieve full self-sustenance: frontier-quality cognition still required paid budget. Strategic depth was bounded by the quality of the seed model.

## Running

```bash
cp .env.example .env
# edit .env: OPENROUTER_API_KEY, BUDGET_USD

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
- **Real budget.** Every LLM call costs real money. The bridge tracks spend. Paid models are disabled at $0; the agent continues only if it has arranged free-tier access.
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
