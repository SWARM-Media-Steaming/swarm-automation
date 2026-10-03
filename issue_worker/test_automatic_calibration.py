"""Issue #374: publication -> CLI eligibility -> pricing -> retirement, without gates."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import available_models as available
import dynamic_router
import model_calibration as calibration
import model_lifecycle
import model_pricing
import model_router


class AutomaticCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = calibration.ModelCalibrationService(Path(self.tmp.name))
        self.env = mock.patch.dict(os.environ, {
            'ARTIFICIAL_ANALYSIS_API_KEY': 'fixture-key',
            'SWARM_MODEL_CALIBRATION_CATALOG': str(self.service.catalog_override_path),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        available.reset()
        self.addCleanup(available.reset)

    def refresh(self, names, rows=None, **kwargs):
        rows = rows if rows is not None else [
            {'provider': 'openai', 'model': 'gpt-6-sol', 'input_cost': 2, 'output_cost': 10,
             'evaluations': {'artificial_analysis_intelligence_index': 50}},
            {'provider': 'openai', 'model': 'gpt-6-1-sol', 'input_cost': 2, 'output_cost': 10,
             'evaluations': {'artificial_analysis_intelligence_index': 51.8}},
        ]
        raw = {'codex': names, 'claude': [], 'grok': []}
        available.configure(raw)
        with mock.patch.object(calibration._sources, 'fetch_source', return_value=(rows, {'kind': 'artificial_analysis', 'status': 'ok'})):
            return self.service.refresh(source='artificial_analysis', available_models=raw,
                                        initiated_by='SCHEDULED', **kwargs)

    def test_dormant_retirement_onboards_and_records_spend_in_one_refresh(self):
        first = self.refresh(['gpt-6-sol'])
        self.assertTrue(first['activated'], first)
        self.assertFalse(available.is_blacklisted('gpt-6-sol'))
        self.assertIn('gpt-6-sol', dynamic_router.catalog_model_names(('codex',)))
        self.assertNotIn('gpt-6-1-sol', dynamic_router.catalog_model_names(('codex',)))
        self.assertEqual(len([line for line in first['log_lines'] if 'not offered' in line]), 1)
        repeat = self.refresh(['gpt-6-sol'], force=True)
        self.assertEqual(repeat['log_lines'], [])
        self.assertEqual(repeat['status'], 'no_change')
        # CLI change bypasses the success interval and requires no user action.
        result = self.refresh(['gpt-6-sol', 'gpt-6-1-sol'])
        self.assertTrue(result['activated'], result)
        self.assertEqual(sum('now available' in line for line in result['log_lines']), 1)
        self.assertEqual(sum('gpt-6-sol retired' in line for line in result['log_lines']), 1)
        self.assertTrue(available.is_blacklisted('gpt-6-sol'))
        self.assertEqual(available.replace_blacklisted('gpt-6-sol'), 'gpt-6-1-sol')
        names = dynamic_router.catalog_model_names(('codex',))
        self.assertIn('gpt-6-1-sol', names)
        self.assertNotIn('gpt-6-sol', names)
        picked = dynamic_router.scored_floor('codex', 4)
        self.assertIsNotNone(picked)
        self.assertNotEqual(picked[0], 'gpt-6-sol')
        estimate = model_pricing.estimate_invocation_cost(model='gpt-6-1-sol', provider='codex',
            input_tokens=1_000_000, output_tokens=1_000_000, cached_input_tokens=500_000,
            cached_tokens_included_in_input=True)
        self.assertEqual(estimate.cost, 12)
        self.assertTrue(estimate.rate_id.startswith('calibration/'))
        retired = next(row for row in self.service.load_active()['models'] if row['model'] == 'gpt-6-sol')
        self.assertFalse(retired['active'])
        self.assertTrue(retired['deprecated'])
        self.assertEqual(retired['superseded_by'], 'gpt-6-1-sol')
        repeat = self.refresh(['gpt-6-sol', 'gpt-6-1-sol'], force=True)
        self.assertEqual(repeat['status'], 'no_change')
        self.assertEqual(repeat['log_lines'], [])

    def test_missing_price_and_missing_cli_never_route(self):
        rows = [{'provider': 'openai', 'model': 'gpt-7-sol', 'evaluations': {'coding': 50}},
                {'provider': 'unsupported', 'model': 'foreign', 'input_cost': 1, 'output_cost': 2}]
        result = self.refresh(['gpt-7-sol'], rows)
        names = dynamic_router.catalog_model_names(('codex',))
        self.assertNotIn('gpt-7-sol', names)
        self.assertNotIn('foreign', names)
        self.assertFalse(any('foreign' in line for line in result['log_lines']))
        active = {row['model']: row for row in self.service.load_active()['models']}
        self.assertNotIn(active['gpt-7-sol']['status'], calibration.ROUTABLE_STATUSES)
        self.assertNotIn('foreign', active)

    def test_regression_activates_and_analysis_is_automatic(self):
        self.refresh(['gpt-6-sol'])
        with mock.patch.object(calibration, 'run_simulation', return_value={'regression_ok': False, 'regressions': ['fixture']}):
            result = self.refresh(['gpt-6-sol', 'gpt-6-1-sol'])
        self.assertTrue(result['activated'])
        self.assertTrue(result['diff']['regression'])
        self.assertIn('regression', result['notification']['message'])
        self.assertIn('analysis', result)
        self.assertEqual(self.service.load_active()['version'], result['calibration_version'])
        self.assertEqual(result['diff']['activation']['policy'], 'auto')
        self.assertEqual(self.service.load_active()['diff']['activation']['version'], result['calibration_version'])

    def test_failed_refresh_preserves_publication_and_backs_off(self):
        self.refresh(['gpt-6-sol'], now=1000)
        before = self.service.catalog_override_path.read_bytes()
        with mock.patch.object(calibration._sources, 'fetch_source', side_effect=calibration._sources.SourceError('offline')):
            failed = self.service.refresh(source='artificial_analysis', force=True, now=1100)
            self.assertEqual(failed['status'], 'failed')
            skipped = self.service.refresh(source='artificial_analysis', now=1200)
            self.assertEqual(skipped['reason'], 'retry_backoff')
            retried = self.service.refresh(source='artificial_analysis', now=2001)
            self.assertEqual(retried['status'], 'failed')
        self.assertEqual(before, self.service.catalog_override_path.read_bytes())

    def test_missing_key_is_explicit_and_never_fetches(self):
        self.refresh(['gpt-6-sol'])
        before = self.service.catalog_override_path.read_bytes()
        with mock.patch.dict(os.environ, {'ARTIFICIAL_ANALYSIS_API_KEY': ''}), mock.patch.object(calibration._sources, 'fetch_source') as fetch:
            result = self.service.refresh(source='artificial_analysis', force=True)
        self.assertEqual(result['status'], 'not_configured')
        fetch.assert_not_called()
        self.assertEqual(before, self.service.catalog_override_path.read_bytes())

    def test_static_prices_win_and_invalid_feed_rates_never_resolve(self):
        import dataclasses
        entry = {'agent': 'codex', 'provider': 'openai', 'model': 'gpt-5.6-sol', 'input_cost': 999, 'output_cost': 999}
        self.assertEqual(model_pricing.resolve_price(entry['model'], calibration_entry=entry).price.input_per_million, 5)
        spec = next(row for row in model_router.load_model_catalog() if row.model == entry['model'])
        spec = dataclasses.replace(spec, input_cost=None, output_cost=None, benchmarks={})
        self.assertEqual(model_router.estimated_dollar_cost(spec, 'low'), 0.06)
        for value in (float('nan'), float('inf'), -1, True, '2'):
            entry.update(model='gpt-9-sol', input_cost=value)
            self.assertFalse(model_pricing.resolve_price(entry['model'], calibration_entry=entry).priced)

    def test_derived_retirement_obeys_price_score_family_and_provider(self):
        def row(name, input_cost=2, score=50, provider='openai'):
            return {'model': name, 'agent': 'codex', 'provider': provider, 'input_cost': input_cost,
                    'output_cost': 10, 'intelligence_by_effort': {'high': score}}
        old = row('gpt-10-test')
        new = row('gpt-10-1-test')
        cli = {'codex': [old['model'], new['model']]}
        self.assertEqual(model_lifecycle.supersessions([old, new], cli), {old['model']: new['model']})
        for bad in (row(new['model'], 2.2), row(new['model'], score=48), row(new['model'], provider='xai'), row('gpt-11-other')):
            self.assertEqual(model_lifecycle.supersessions([old, bad], cli), {})
        self.assertEqual(model_lifecycle.supersessions([old, new], {'codex': [old['model']]}), {})

    def test_empty_cli_filter_stays_empty(self):
        self.assertEqual(calibration._available_from_json('{}'), {})
        result = self.refresh([], force=True)
        active = self.service.load_active()['models']
        self.assertFalse(any(row['status'] in calibration.ROUTABLE_STATUSES for row in active))
        self.assertFalse(any(row['model'] == 'gpt-6-1-sol' and row['status'] in calibration.ROUTABLE_STATUSES for row in active))
        self.assertEqual([line for line in result['log_lines'] if 'gpt-6-1-sol is reported' in line], [
            'Model data: openai/gpt-6-1-sol is reported by Artificial Analysis but not offered by the codex CLI; not a routing candidate.',
        ])

    def test_scheduler_live_reload_replaces_stale_discovery_and_selections(self):
        import install_swarm_issue_cron as scheduler
        args = ['--available-models', '{"codex":["gpt-6-1-sol"]}',
                '--codex-model', 'gpt-6-1-sol', '--codex-router-model', 'gpt-6-1-sol',
                '--codex-effort', 'high', '--codex-router-effort', 'medium']
        self.assertEqual(scheduler.saved_routing_overrides(args), args)

    def test_started_session_keeps_retired_model(self):
        from types import SimpleNamespace
        import swarm_issue_worker
        self.refresh(['gpt-6-sol', 'gpt-6-1-sol'])
        pinned = SimpleNamespace(key='codex', model='gpt-6-sol', effort='high', resume=True)
        fresh = SimpleNamespace(key='codex', model='gpt-6-1-sol', effort='high', resume=False)
        switch, reason = swarm_issue_worker.Worker.reroute_verdict(None, pinned, fresh, True)
        self.assertFalse(switch)
        self.assertIn('pinned', reason)

    def test_conflicting_aa_duplicates_keep_last_good_calibration(self):
        self.refresh(['gpt-6-sol'])
        before = self.service.catalog_override_path.read_bytes()
        rows = [{'provider': 'openai', 'model': 'gpt-6-sol', 'input_cost': 2},
                {'provider': 'openai', 'model': 'gpt-6-sol', 'input_cost': 20}]
        for ordered in (rows, rows[::-1]):
            result = self.refresh(['gpt-6-sol'], ordered, force=True)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(before, self.service.catalog_override_path.read_bytes())

    def test_all_blacklist_entries_require_an_offered_priced_successor(self):
        rows = calibration.fetch_local_source()
        for old, successor in available.listed_retirements().items():
            with self.subTest(old=old):
                agent = 'codex' if old.startswith('gpt-') else 'claude'
                dormant = model_lifecycle.policy_snapshot(rows, {agent: [old]})
                self.assertNotIn(old, dormant['retirements'])
                priced_rows = rows + [{'model': successor, 'agent': agent,
                                      'input_cost': 2, 'output_cost': 10}]
                active = model_lifecycle.policy_snapshot(priced_rows, {agent: [old, successor]})
                self.assertEqual(active['retirements'][old], successor)

    def test_withdrawn_successor_is_not_resurrected_from_stale_cli_evidence(self):
        self.refresh(['gpt-6-sol', 'gpt-6-1-sol'])
        # Simulate a worker that was configured before the next publication.
        stale = available._discovered_rows()
        result = self.refresh(['gpt-6-sol'], force=True)
        with mock.patch.object(available, '_models', stale), mock.patch.object(available, '_configured_version', 'older'):
            self.assertIn('gpt-6-sol', dynamic_router.catalog_model_names(('codex',)))
            self.assertNotIn('gpt-6-1-sol', dynamic_router.catalog_model_names(('codex',)))
            self.assertFalse(available.is_blacklisted('gpt-6-sol'))
        self.assertEqual(sum('gpt-6-1-sol is reported' in line for line in result['log_lines']), 1)
        self.assertEqual(self.refresh(['gpt-6-sol'], force=True)['log_lines'], [])

    def test_empty_new_publication_replaces_all_stale_worker_discovery(self):
        self.refresh(['gpt-6-sol', 'gpt-6-1-sol'])
        available.configure({'codex': ['gpt-6-sol', 'gpt-6-1-sol']})
        rows = [{'provider': 'openai', 'model': 'gpt-6-sol', 'input_cost': 2, 'output_cost': 10}]
        with mock.patch.object(calibration._sources, 'fetch_source', return_value=(rows, {'status': 'ok'})):
            result = self.service.refresh(source='artificial_analysis', available_models={}, force=True)
        self.assertTrue(result['activated'])
        self.assertEqual(available.discovered(), ())
        self.assertEqual(dynamic_router.catalog_model_names(), ())

    def test_feed_only_derived_retirement_reaches_all_routing_consumers(self):
        import decision_engine
        rows = [{'provider': 'openai', 'model': name, 'input_cost': 2, 'output_cost': 10,
                 'evaluations': {calibration.INTELLIGENCE_KEY: score}}
                for name, score in [('gpt-10-sol', 51), ('gpt-10-1-sol', 52)]]
        self.refresh(['gpt-10-sol'], rows[:1])
        # The old release remains as a peer even when the next feed omits it.
        result = self.refresh(['gpt-10-sol', 'gpt-10-1-sol'], rows[1:], force=True)
        self.assertTrue(result['activated'])
        self.assertIn({'model': 'gpt-10-sol', 'superseded_by': 'gpt-10-1-sol'}, result['diff']['supersessions'])
        old = next(row for row in self.service.load_active()['models'] if row['model'] == 'gpt-10-sol')
        self.assertFalse(old['active'])
        self.assertTrue(old['deprecated'])
        self.assertEqual(dynamic_router.catalog_model_names(('codex',)), ('gpt-10-1-sol',))
        self.assertEqual([row['model'] for row in decision_engine.routable_models_for_jev()['codex']], ['gpt-10-1-sol'])
        tiers = dynamic_router.derived_routing_tiers('codex')
        self.assertTrue(tiers)
        self.assertEqual({tier.model for tier in tiers}, {'gpt-10-1-sol'})
        candidate = dynamic_router.RouterCandidate(key='codex', name='Codex', tiers=tiers)
        prompt = '\n'.join(dynamic_router.catalog_prompt_lines([candidate]))
        self.assertIn(' / gpt-10-1-sol ', prompt)
        self.assertNotIn(' / gpt-10-sol ', prompt)
        self.assertEqual(dynamic_router.latest_release('codex', 'gpt-10-sol', 'high').model, 'gpt-10-1-sol')

    def test_more_expensive_or_weaker_release_does_not_retire_predecessor(self):
        rows = [{'provider': 'openai', 'model': name, 'input_cost': cost, 'output_cost': 10,
                 'evaluations': {calibration.INTELLIGENCE_KEY: score}}
                for name, cost, score in [('gpt-10-sol', 2, 51), ('gpt-10-1-sol', 3, 52)]]
        for cost, score in [(3, 52), (2, 48)]:
            rows[1].update(input_cost=cost, evaluations={calibration.INTELLIGENCE_KEY: score})
            self.refresh(['gpt-10-1-sol', 'gpt-10-sol'], rows, force=True)
            self.assertFalse(available.is_blacklisted('gpt-10-sol'))
            self.assertIn('gpt-10-sol', dynamic_router.catalog_model_names(('codex',)))

    def test_price_arrival_onboards_without_another_human_action(self):
        row = {'provider': 'openai', 'model': 'gpt-9-sol', 'evaluations': {calibration.INTELLIGENCE_KEY: 52}}
        self.refresh(['gpt-9-sol'], [row])
        self.assertNotIn('gpt-9-sol', dynamic_router.catalog_model_names(('codex',)))
        row.update(input_cost=2, output_cost=10)
        result = self.refresh(['gpt-9-sol'], [row], force=True)
        self.assertTrue(result['activated'])
        self.assertTrue(result['notification']['should_notify'])
        self.assertIn('gpt-9-sol', dynamic_router.catalog_model_names(('codex',)))

    def test_derived_retirement_can_skip_a_dormant_explicit_successor(self):
        rows = [{'provider': 'openai', 'model': name, 'input_cost': 2, 'output_cost': 10,
                 'evaluations': {calibration.INTELLIGENCE_KEY: 52}}
                for name in ['gpt-6-sol', 'gpt-6-2-sol']]
        self.refresh(['gpt-6-sol', 'gpt-6-2-sol'], rows)
        self.assertEqual(available.blacklist_successor('gpt-6-sol'), 'gpt-6-2-sol')
        self.assertEqual(dynamic_router.catalog_model_names(('codex',)), ('gpt-6-2-sol',))

    def test_failed_publication_does_not_consume_availability_transition(self):
        self.refresh(['gpt-6-sol'])
        before = self.service.catalog_override_path.read_bytes()
        with mock.patch.object(self.service, '_write_catalog_override', side_effect=OSError('fixture publication failure')):
            failed = self.refresh(['gpt-6-sol', 'gpt-6-1-sol'], force=True)
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(before, self.service.catalog_override_path.read_bytes())
        retried = self.refresh(['gpt-6-sol', 'gpt-6-1-sol'], force=True)
        self.assertEqual(sum('now available' in line for line in retried['log_lines']), 1)
        self.assertEqual(sum('gpt-6-sol retired' in line for line in retried['log_lines']), 1)

    def test_analysis_failure_does_not_fail_an_activation(self):
        with mock.patch.object(self.service, 'analyze', side_effect=RuntimeError('fixture')):
            result = self.refresh(['gpt-6-sol', 'gpt-6-1-sol'])
        self.assertTrue(result['activated'])
        self.assertEqual(result['status'], 'changed')

    def test_feed_price_covers_the_cli_dated_alias_and_provider_namespace(self):
        row = {'provider': 'anthropic', 'model': 'claude-quartz-9', 'input_cost': 2, 'output_cost': 10}
        report = {'claude': ['claude-quartz-9-20260930'], 'codex': [], 'grok': []}
        available.configure(report)
        with mock.patch.object(calibration._sources, 'fetch_source', return_value=([row], {'status': 'ok'})):
            result = self.service.refresh(source='artificial_analysis', available_models=report)
        self.assertTrue(result['activated'])
        price = model_pricing.resolve_price('claude-quartz-9-20260930', provider='claude')
        self.assertTrue(price.priced)
        self.assertEqual(price.price.input_per_million, 2)
        self.assertFalse(model_pricing.resolve_price('claude-quartz-9-20260930', provider='codex').priced)
        self.assertEqual(dynamic_router.catalog_model_names(('claude',)), ('claude-quartz-9',))
        candidate = dynamic_router.RouterCandidate(key='claude', name='Claude',
            tiers=dynamic_router.derived_routing_tiers('claude'))
        self.assertEqual([entry.model for entry in dynamic_router.candidate_catalog(candidate)], ['claude-quartz-9'])
        self.assertIn(' / claude-quartz-9 ', '\n'.join(dynamic_router.catalog_prompt_lines([candidate])))

    def test_static_ambiguity_is_not_masked_by_feed_rates(self):
        import dataclasses
        self.refresh(['gpt-6-sol'])
        spec = next(row for row in model_router.load_model_catalog() if row.model == 'gpt-5.6-sol')
        spec = dataclasses.replace(spec, input_cost=2, output_cost=10)
        duplicate = next(price for price in model_pricing.PRICING_CATALOG if price.model == spec.model)
        with mock.patch.object(model_pricing, 'PRICING_CATALOG', model_pricing.PRICING_CATALOG + (duplicate,)):
            self.assertFalse(model_pricing.resolve_price(spec.model).priced)
            self.assertFalse(model_router.is_priced(spec))

    def test_all_initiators_default_to_auto_and_approval_state_is_removed(self):
        parser = calibration.build_calibration_parser()
        self.assertEqual(parser.parse_args(['--state-dir', self.tmp.name, 'refresh']).activation_policy, 'auto')
        self.service.save_state({'approved_models': ['openai/gpt-6-1-sol'], 'last_approved_by': 'USER'})
        self.assertNotIn('approved_models', self.service.load_state())
        self.assertNotIn('last_approved_by', self.service.load_state())
        self.assertFalse(hasattr(self.service, 'approve_discovered_model'))
        for index, initiator in enumerate(calibration.ALLOWED_INITIATORS):
            with self.subTest(initiator=initiator):
                with mock.patch.object(calibration._sources, 'fetch_source', return_value=([
                    {'provider': 'openai', 'model': 'gpt-9-sol', 'input_cost': 2 + index, 'output_cost': 10},
                ], {'status': 'ok'})):
                    result = self.service.refresh(source='artificial_analysis', available_models={'codex': ['gpt-9-sol']},
                                                  initiated_by=initiator, force=True)
                self.assertTrue(result['activated'])

    def test_feed_spend_is_recorded_with_rate_provenance(self):
        from types import SimpleNamespace
        import swarm_issue_worker
        import token_usage
        self.refresh(['gpt-6-sol', 'gpt-6-1-sol'])
        worker = mock.Mock()
        worker.history = SimpleNamespace(execution_id='fixture')
        worker.choice = None
        worker.issue = None
        worker.current_token_usage_events.return_value = []
        swarm_issue_worker.Worker._record_usage_event(worker, agent_type='IMPLEMENTER', prompt_type='IMPLEMENTATION',
            provider_key='codex', provider_name='Codex', model='gpt-6-1-sol', effort='high', attempt_number=1,
            usage=token_usage.NormalizedUsage(input_tokens=1_000_000, output_tokens=1_000_000),
            started_at='2026-09-30T00:00:00+00:00', success=True, error_type='')
        event = worker._append_token_usage_event.call_args.args[0]
        self.assertEqual(event['estimated_cost'], 12)
        self.assertEqual(event['input_rate_per_million'], 2)
        self.assertEqual(event['output_rate_per_million'], 10)
        self.assertTrue(event['pricing_rate_id'].startswith('calibration/'))


if __name__ == '__main__':
    unittest.main()
