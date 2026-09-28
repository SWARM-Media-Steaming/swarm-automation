# Issue 205 independent acceptance oracles

Derived from the issue and domain invariants before examining implementation:

- AC 5–6, 15, 17: startup and refresh preserve the last known-good routing
  catalog; a first remote refresh also cannot silently replace the bundled
  catalog under a manual activation policy. Status-page timing must not decide
  whether external data is activated.
- Manual refresh / discovery requirements: an unreviewed model stays unavailable
  until deliberate approval, even when the live catalog contains no eligible
  model for a provider. Reusing stale bundled data must not bypass that gate.
- AC 12–13 and Refresh Change Summary: a pending proposal must retain accessible
  detailed changes and simulation results after an unchanged check or activation.
  An unchanged observation is not the same as having no proposal to review.
- AC 3, 9, 13: source observations must be normalized consistently; missing or
  invalid fields must not fabricate prices, healthy coverage, or model identities.
- AC 16: rollback restores the actual model data, eligibility and routing
  behavior, including immediately after a later proposal or failed refresh.

All executable fixtures will be local and deterministic. Existing expectations
and security-owned tests will remain unchanged. Only observed in-scope defects
will receive blocking assertions; unrelated failures will be reported separately.
