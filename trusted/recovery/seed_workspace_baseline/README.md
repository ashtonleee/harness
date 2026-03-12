# agent_workspace

This directory is the only mutable seed repo/workspace for Stage 4.

- Inside the container, it is mounted at `/workspace/agent`.
- The static runtime and harness code lives under `/app/untrusted`.
- The seed runner may read and write files here, but it must not treat `/app/untrusted` as the self-edit target.
- Trusted recovery resets this workspace back to the seed baseline in `trusted/recovery/seed_workspace_baseline/`.

Local validation from inside the agent workspace:

```bash
python -m pytest -q
```

Generated run artifacts go under `run_outputs/` and are ignored by git.
