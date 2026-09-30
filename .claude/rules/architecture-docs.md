# Interactive Architecture Documentation Rules

`architecture_docs_enabled` is a repository setting, off by default. When on,
the worker keeps one structured architecture model per repository, and the
Architecture view renders it for five audiences (engineer, architect,
cybersecurity, product, executive) without separate documents.

- The model, validation, redaction, impact signals, prompt and the worker
  mixin all live in `issue_worker/architecture_docs.py`; rendering is the pure
  `ui/architecture-docs.js`. Change behavior there, not in a parallel path.
- Storage is `<worker_state_dir>/architecture_docs/<repo>.json`, in
  application state. Never write documentation into the monitored repository.
- The AI returns a bounded structured patch (`validate_review`), never HTML.
  It cannot claim `human` provenance, and human-authored entities are never
  overwritten or removed by AI patches. Observed statements need evidence.
- Deterministic `impact_signals` gate the AI pass. No signals, or an AI "none",
  records a lightweight no-change review and rewrites nothing.
- A patch is applied and `documentedThrough` advanced only when the change is
  merged into the integration branch (`merged=` in `finalize_issue`, never on
  a best-effort cap-hit). Otherwise it stays in `pending` and is reconciled
  with `commit_on_integration_branch` on a later review. Failed, paused or
  quota-limited reviews record `failed` and change nothing.
- Everything sent to the AI and everything stored goes through `redact`
  (secrets, URLs, IPs, key blocks) and `safe_repo_path` (no `.env`, keys,
  traversal). The review runs after delivery and must never raise into it; any
  repository edit made by the review session is discarded.
- This setting changes behavior, so it needs the `minor` label from a trusted
  author; the worker owns VERSION.
