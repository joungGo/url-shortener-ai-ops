"""Offline WF10 change plan: data-only apply/rollback bound to one verified candidate."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_ops.wf10_change_plan import APPLY_ORDER, PROTECTED, build, main, plan, verify
from ai_ops.wf10_deployment import build as build_candidate, draft
from ai_ops.wf10_preparation import digest, encoded
from test_wf10_deployment import fixture


class ChangePlanTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {'AI_OPS_FILES_ROOT': str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.candidate = self.root / 'candidate'
        build_candidate(fixture(), self.candidate)
        self.resources = json.loads((self.candidate / 'candidate.json').read_text())['resources']

    def test_apply_order_and_per_resource_hashes(self):
        p = plan(self.candidate)
        order = [(s['kind'], s['name']) for s in p['apply']]
        self.assertEqual(order, [(k, 'wf10-fixture' + s) for k, s in APPLY_ORDER])
        kinds = [k for k, _ in order]
        self.assertLess(max(i for i, k in enumerate(kinds) if k == 'NetworkPolicy'),
                        min(i for i, k in enumerate(kinds) if k == 'Deployment'))
        self.assertLess(order.index(('Deployment', 'wf10-fixture-sender')), order.index(('Deployment', 'wf10-fixture')))
        by_name = {(r['kind'], r['metadata']['name']): r for r in self.resources}
        for step in p['apply']:
            self.assertEqual(step['manifest_sha256'], digest(encoded(by_name[(step['kind'], step['name'])])))
            self.assertIn('server_side_dry_run_passed', step['requires'])
        windows = {s['name']: s.get('startup_window_seconds') for s in p['apply'] if s['kind'] == 'Deployment'}
        self.assertEqual(windows, {'wf10-fixture': 120, 'wf10-fixture-sender': 60})  # derived from probes

    def test_rollback_reverses_only_planned_resources_and_is_hash_bound(self):
        p = plan(self.candidate)
        rollback = p['rollback']
        self.assertEqual([(s['kind'], s['name']) for s in rollback['steps']],
                         [(s['kind'], s['name']) for s in reversed(p['apply'])])
        self.assertTrue(all(s['action'] == 'delete_if_recorded_uid_matches' for s in rollback['steps']))
        self.assertTrue(rollback['match_rule']['recorded_uid_required'])
        for kind in ('PersistentVolumeClaim', 'Secret', 'Namespace', 'ServiceAccount'):
            self.assertIn(kind, rollback['never_delete_or_modify'])
            self.assertNotIn(kind, [s['kind'] for s in rollback['steps']])
        self.assertEqual(p['rollback_bundle_sha256'], digest(encoded(rollback)))
        self.assertFalse(rollback['execution_allowed'])

    def test_plan_is_data_only_and_never_grants_execution(self):
        p = plan(self.candidate)
        text = encoded(p).decode()
        for forbidden in ('kubectl', 'helm ', 'bash', ' sh ', 'curl', 'fixture-bot', 'fixture-signing'):
            self.assertNotIn(forbidden, text)
        self.assertFalse(p['execution_allowed'])
        self.assertFalse(p['live_verified'])
        self.assertFalse(p['approval_binding']['granted'])
        self.assertFalse(p['approval_binding']['reuse_of_past_approvals'])
        self.assertIn('server_side_dry_run', p['required_live_gates'])
        self.assertEqual(p['candidate']['manifest_sha256'],
                         json.loads((self.candidate / 'candidate.json').read_text())['manifest_sha256'])

    def test_deterministic_package_modes_and_tamper_detection(self):
        out = self.root / 'plan'
        result = build(self.candidate, out)
        self.assertEqual(result, {**result, 'execution_allowed': False})
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)
        self.assertEqual((out / 'change-plan.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(verify(self.candidate, out)['change_plan_sha256'], result['change_plan_sha256'])
        with self.assertRaises(FileExistsError):
            build(self.candidate, out)
        doc = json.loads((out / 'change-plan.json').read_text())
        doc['rollback']['steps'].pop()  # silently drop one rollback delete
        doc['rollback_bundle_sha256'] = digest(encoded(doc['rollback']))
        (out / 'change-plan.json').write_bytes(encoded(doc))
        with self.assertRaisesRegex(ValueError, 'plan_changed'):
            verify(self.candidate, out)

    def test_changed_or_incomplete_candidate_is_rejected(self):
        p = self.candidate / 'candidate.json'
        doc = json.loads(p.read_text())
        doc['resources'][0]['data']['slack-policy.json'] = '{}'
        p.write_bytes(encoded(doc))
        with self.assertRaises(ValueError):
            plan(self.candidate)
        self.assertEqual(build_candidate(draft(), self.root / 'none')['status'], 'blocked_missing_inputs')
        with self.assertRaises(OSError):
            plan(self.root / 'none')

    def test_protected_kinds_cover_preexisting_inputs(self):
        for kind in ('Namespace', 'PersistentVolumeClaim', 'Secret', 'ServiceAccount', 'RoleBinding', 'Ingress'):
            self.assertIn(kind, PROTECTED)
        self.assertFalse({r['kind'] for r in self.resources} & set(PROTECTED))

    def test_cli_rejection_is_generic(self):
        output = io.StringIO()
        with redirect_stdout(output):
            rc = main(['build', '--candidate', str(self.root / 'missing-secret-path'), '--output', str(self.root / 'x')])
        self.assertEqual(rc, 1)
        self.assertEqual(output.getvalue().strip(), '{"status":"rejected","execution_allowed":false}')
        self.assertFalse((self.root / 'x').exists())
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            main(['apply'])


if __name__ == '__main__':
    unittest.main()
