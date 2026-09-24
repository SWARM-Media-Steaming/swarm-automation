// Issue #278 AC 12/13/17: attempted-but-failed reviews stay visible to operators.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const security = require('../../../ui/adversarial-security.js');
const {deriveNowWorking} = require('../../../ui/now-working.js');

test('an execution failure before any completed round displays FAILED', () => {
  // The Python failure_history_fields path writes a status/error without a
  // security_outcome, because no round completed. This is the desktop payload.
  assert.equal(security.reviewStatus({securityOutcome: '', securityReviewStatus: 'FAILED',
    securityReviewError: 'the adversarial coding session failed'}), 'FAILED');
});

test('disabled and legacy executions remain distinct from failed review attempts', () => {
  assert.equal(security.reviewStatus({}), '—');
  assert.equal(security.reviewStatus({securityOutcome: 'disabled'}), '—');
  assert.equal(security.reviewStatus({securityOutcome: 'clean_first_pass', securityReviewStatus: 'PASS'}), 'PASS');
});

const repo = {id: 'local', name: 'fixture/app', monitorActions: false};
const line = message => `[12:34:56] [Issue worker scheduler/stdout] [2026-09-24 12:34:56-0500] ${message}`;
const started = [
  line('Selected oldest unprocessed assigned issue: #278 Restrict record reads'),
  line('Adversarial Cybersecurity for issue #278: starting independent security review (round 0 of 6).'),
];

test('the live security row becomes failing when the reviewer fails', () => {
  const rows = deriveNowWorking({repositories: [repo], workerState: 'running', logs: [
    ...started, line('Adversarial Cybersecurity for issue #278: review failed — the adversarial coding session failed'),
  ]});
  const review = rows.find(row => row.kind === 'security');
  assert.ok(review, 'Keep an actionable failed review visible');
  assert.equal(review.state, 'error', 'A failed review must not remain displayed as running');
});

test('a completed security review is no longer shown as actively running', () => {
  const rows = deriveNowWorking({repositories: [repo], workerState: 'running', logs: [
    ...started, line('Adversarial Cybersecurity for issue #278: review completed with status PASS.'),
  ]});
  const review = rows.find(row => row.kind === 'security');
  assert.ok(!review || review.state !== 'running', 'Completion must end the active review indicator');
});
