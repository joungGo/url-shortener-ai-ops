"""WF10 renderer: optional recovery.publish (WF11 A2, 2026-11-27).

The relay opens the WF07 PR with a short-lived C03 writer App installation token. Only the
slack-relay container holds it; the rendered recovery config points prepare.publish_token_file
at it. Network and other Pods are unchanged. Absent -> output unchanged.
"""
from copy import deepcopy
import json
import unittest

import test_wf10_recovery_render as render
from ai_ops.recovery_approval_wiring import RecoveryApprovalConfig
from ai_ops.wf10_deployment import candidate

SECRET = 'fixture-pr-writer'


class PublishRenderTest(unittest.TestCase):
    def setUp(self):
        self.r = render.RecoveryRenderTest()
        self.r.setUp()
        self.addCleanup(self.r.doCleanups)
        self.plain = self.r.v
        self.v = deepcopy(self.plain)
        self.v['recovery']['publish'] = {'secret': SECRET}
        self.result = candidate(self.v)

    def pod(self, result, suffix=''):
        return next(r for r in result['resources'] if r['kind'] == 'Deployment'
                    and r['metadata']['name'] == 'wf10-fixture' + suffix)['spec']['template']['spec']

    def recovery_config(self, result):
        config = next(r for r in result['resources'] if r['kind'] == 'ConfigMap'
                      and r['metadata']['name'] == 'wf10-fixture-config')
        return json.loads(config['data']['recovery-approval.json'])

    def test_relay_alone_holds_the_token_and_the_config_points_at_it(self):
        pod = self.pod(self.result)
        source = next(v for v in pod['volumes'] if v['name'] == 'pr-writer-source')
        self.assertEqual(source['secret']['secretName'], SECRET)
        self.assertEqual(source['secret']['items'], [{'key': 'token', 'path': 'token'}])
        holders = [c['name'] for c in pod['containers'] if any(m['name'] == 'pr-writer' for m in c['volumeMounts'])]
        self.assertEqual(holders, ['slack-relay'])
        [mount] = [m for c in pod['containers'] for m in c['volumeMounts'] if m['name'] == 'pr-writer']
        self.assertEqual((mount['mountPath'], mount['readOnly']), ('/run/ai-pr-writer', True))
        prepare = self.recovery_config(self.result)['prepare']
        self.assertEqual(prepare['publish_token_file'], '/run/ai-pr-writer/token')
        RecoveryApprovalConfig.model_validate(self.recovery_config(self.result))  # runtime accepts it
        self.assertEqual(json.dumps(self.result['resources']).count(SECRET), 1)
        for suffix in ('-sender', '-egress'):
            self.assertNotIn('pr-writer', json.dumps(self.pod(self.result, suffix)))

    def test_absent_publish_keeps_prepare_only(self):
        plain = candidate(deepcopy(self.plain))
        self.assertIsNone(self.recovery_config(plain)['prepare'].get('publish_token_file'))
        self.assertNotIn('pr-writer', json.dumps(plain['resources']))
        self.assertNotIn('publish', plain['recovery'])

    def test_network_and_other_pods_unchanged(self):
        plain = candidate(deepcopy(self.plain))
        policies = lambda result: {r['metadata']['name']: r['spec'] for r in result['resources']
                                   if r['kind'] == 'NetworkPolicy'}
        self.assertEqual(policies(self.result), policies(plain))
        for suffix in ('-sender', '-egress'):
            self.assertEqual(self.pod(self.result, suffix), self.pod(plain, suffix))
        self.assertEqual(self.result['recovery']['publish'], 'c03_writer_app_pr_after_prepare_human_merge')
        self.assertTrue(any('writer App installation token' in x for x in self.result['external_prerequisites']))
        self.assertTrue(any('merge still needs the protected human review' in x for x in self.result['residual_risks']))

    def test_input_validation(self):
        cases = {'extra_field': {'secret': SECRET, 'merge': True}, 'missing_secret': {}, 'bad_name': {'secret': 'Bad_Name'},
                 'reuses_github_secret': {'secret': self.plain['recovery']['secrets']['github']},
                 'reuses_main_secret': {'secret': self.plain['secrets']['bot']}, 'not_a_dict': ['x']}
        for name, value in cases.items():
            v = deepcopy(self.plain)
            v['recovery']['publish'] = value
            with self.subTest(case=name), self.assertRaises(ValueError):
                candidate(v)
        for key in ('pub', 'publisher', 'publish_pr', 'writer'):  # unknown recovery keys stay rejected
            v = deepcopy(self.plain)
            v['recovery'][key] = {'secret': SECRET}
            with self.subTest(key=key), self.assertRaises(ValueError):
                candidate(v)
        v = deepcopy(self.plain)
        v['recovery']['publish'] = {'secret': SECRET}
        v['recovery']['ci_relay'] = {'secret': SECRET}  # the two tokens must differ
        with self.assertRaises(ValueError):
            candidate(v)
        v = deepcopy(self.plain)
        v['recovery']['publish'] = {'secret': SECRET}
        v['recovery']['ci_relay'] = {'secret': 'fixture-ci-github'}
        both = candidate(v)
        self.assertEqual(both['recovery']['publish'], 'c03_writer_app_pr_after_prepare_human_merge')
        self.assertEqual(both['recovery']['ci_relay'], 'normal_ci_receipts_via_egress_proxy')


if __name__ == '__main__':
    unittest.main()
