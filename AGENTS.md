# Working on this repository (for coding agents and people)

- Python 3.12 with uv: `uv sync`, `uv run pytest`, `uv run ruff check src`. Node only under `generators/remotion/`.
- Read `docs/ARCHITECTURE.md` first, then `docs/CANVAS.md` for the composition format.
- Keep the harness general. Do not add layout templates or recipe-specific branches to the core; add a tool, an
  advisor or a check, and put request-type knowledge in `recipes/<name>/SKILL.md`.
- Every new tool goes through `vwb.tools.base.Registry.call` (role allowlist, policy dial, call log, loop guards). Give
  it a capability row in `config/policy.yaml` and add it to `docs/TOOLS_CHECKLIST.md`.
- Every new flag kind goes in `config/flag_kinds.yaml`; code sets severity, agents never do.
- Wrappers around media tools assert their own postconditions (output exists, frame counts match).
- Never put vendor keys in code, images or agent environments; the gateway holds them.
- Runs and caches live in `runs/` (ignored by git).
