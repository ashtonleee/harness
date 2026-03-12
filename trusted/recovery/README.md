# trusted/recovery

Trusted Stage 4 recovery helpers for the mutable workspace only.

- `seed_workspace_baseline/` is the reproducible reset target for `untrusted/agent_workspace/`.
- `store.py` creates compressed workspace checkpoints under `runtime/trusted_state/checkpoints/`.
- `cli.py` is the host-side operator entrypoint used by `scripts/recovery.sh`.

These recovery controls are intentionally not exposed as mutating bridge endpoints yet because Stage 4 still has no real operator/agent authorization split on the bridge.
