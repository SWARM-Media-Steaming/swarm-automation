# Native CLI session and cache optimization

Follow `docs/prompt-caching.md` for session lifecycle and reporting semantics.
Session selection lives at `Worker.run_ai` through `PromptSessionMixin`, after
routing and escalation. Do not add a cache toggle, direct inference API, source
snapshot cache, or alternate telemetry store.

Never share sessions between repositories/issues or with independent UAT/security
reviewers or the architecture documentation review. Each new assessment is fresh.
Only interrupted same-phase reviews may resume; fixer continuity stays within
its own stage/epoch/model/effort. Use an explicit validated UUID and current
source/tests/findings; never use `--last`. Documentation-review usage is
`agent_type=documentation`, not Primary.

Extend `token_usage.py`, `ai_token_usage` and `usage_report.py` for telemetry.
Missing counters remain unavailable. Codex cache reads are included in input;
Claude cache reads/writes are additive. Avoid cumulative-session double counting.
Reported costs, estimated API-equivalent savings and subscription billing are
different things. Cache-write premiums reduce estimated savings.

Cache-based cost adjustments require the evidence gates in `usage_report.py`.
They must not override capability, reasoning effort, escalation, manual settings,
review independence, or existing model fallback behavior. Keep lifecycle/recovery
and legacy-history tests in the regular worker suite. Do not modify independent
adversarial tests to satisfy the implementation.
