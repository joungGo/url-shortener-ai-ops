"""WF10 renderer: optional recovery.ci_relay (PR-b, user decision P1+N1, 2026-11-26).

The CI relay runs as a container in the main Pod (same store as the worker, which turns
normal-CI receipts into WF07 evidence). Only it holds the Actions read token; it reaches
GitHub only through the fixed-destination egress proxy, which then also relays GitHub
artifact storage. Absent -> output byte-identical to the plain recovery render.
"""
from copy import deepcopy
import json
import unittest

import test_wf10_recovery_render as render
from ai_ops.wf10_deployment import candidate

SECRET = 'fixture-ci-github'


class CIRelayRenderTest(unittest.TestCase):
    def setUp(self):
        self.r = render.RecoveryRenderTest()
        self.r.setUp()
        self.addCleanup(self.r.doCleanups)
        self.plain = self.r.v
        self.v = deepcopy(self.plain)
        self.v['recovery']['ci_relay'] = {'secret': SECRET}
        self.result = candidate(self.v)

    def deployment(self, result, suffix=''):
        return next(r for r in result['resources'] if r['kind'] == 'Deployment'
                    and r['metadata']['name'] == 'wf10-fixture' + suffix)['spec']['template']['spec']

    def policy(self, result, suffix=''):
        return next(r for r in result['resources'] if r['kind'] == 'NetworkPolicy'
                    and r['metadata']['name'] == 'wf10-fixture' + suffix)['spec']

    def test_absent_ci_relay_keeps_the_recovery_render_identical(self):
        self.assertEqual(candidate(deepcopy(self.plain))['manifest_sha256'],
                         candidate(deepcopy(self.plain))['manifest_sha256'])
        plain = candidate(deepcopy(self.plain))
        self.assertNotIn('ci-relay', [c['name'] for c in self.deployment(plain)['containers']])
        self.assertEqual(self.deployment(plain, '-egress')['containers'][0]['args'], [])
        self.assertNotIn('ci_relay', plain['recovery'])

    def test_relay_container_contract(self):
        pod = self.deployment(self.result)
        [relay] = [c for c in pod['containers'] if c['name'] == 'ci-relay']
        self.assertEqual(relay['command'], ['python', '-m', 'ai_ops.ci_relay'])
        self.assertEqual(relay['args'], ['--config', '/etc/ai-ops', '--store', '/data/incidents', '--watch',
                                         '--normal-binding', '/etc/ai-ops/normal-ci-binding.json'])
        env = {e['name']: e['value'] for e in relay['env']}
        self.assertEqual(env['AI_CI_GITHUB_TOKEN_FILE'], '/run/ci-github/token')
        self.assertEqual(env['AI_CI_EGRESS_PROXY'], 'http://wf10-fixture-egress.ai-ci.svc:8080')
        mounts = {m['mountPath']: m['name'] for m in relay['volumeMounts']}
        self.assertEqual(mounts['/etc/ai-ops'], 'config')   # copied config incl. the normal binding
        self.assertEqual(mounts['/data'], 'data')           # same store as the worker
        self.assertEqual(mounts['/run/ci-github'], 'ci-github')
        self.assertTrue(all(m['readOnly'] for m in relay['volumeMounts'] if m['mountPath'] != '/data'
                            and m['mountPath'] != '/tmp'))
        self.assertNotIn('livenessProbe', relay)
        config = next(r for r in self.result['resources'] if r['kind'] == 'ConfigMap'
                      and r['metadata']['name'] == 'wf10-fixture-config')
        self.assertIn('normal-ci-binding.json', config['data'])

    def test_only_the_relay_holds_the_actions_token(self):
        pod = self.deployment(self.result)
        source = next(v for v in pod['volumes'] if v['name'] == 'ci-github-source')
        self.assertEqual(source['secret']['secretName'], SECRET)
        self.assertEqual(source['secret']['items'], [{'key': 'token', 'path': 'token'}])
        holders = [c['name'] for c in pod['containers'] if any(m['name'] == 'ci-github' for m in c['volumeMounts'])]
        self.assertEqual(holders, ['ci-relay'])
        everything = json.dumps(self.result['resources'])
        self.assertEqual(everything.count(SECRET), 1)
        for suffix in ('-sender', '-egress'):
            self.assertNotIn('ci-github', json.dumps(self.deployment(self.result, suffix)))

    def test_network_stays_proxy_only(self):
        plain = candidate(deepcopy(self.plain))
        # Main Pod egress is unchanged: no internet rule, only DNS/collector/sender/egress proxy.
        self.assertEqual(self.policy(self.result)['egress'], self.policy(plain)['egress'])
        self.assertFalse(any(b.get('ipBlock', {}).get('cidr') == '0.0.0.0/0'
                             for rule in self.policy(self.result)['egress'] for b in rule['to']))
        self.assertEqual(self.deployment(self.result, '-egress')['containers'][0]['args'], ['--github-artifacts'])
        self.assertEqual(self.policy(self.result, '-egress'), self.policy(plain, '-egress'))
        self.assertEqual(self.result['recovery']['ci_relay'], 'normal_ci_receipts_via_egress_proxy')
        self.assertTrue(any('artifact storage' in x for x in self.result['residual_risks']))
        self.assertTrue(any('F-Lab-DevOps Actions' in x for x in self.result['external_prerequisites']))

    def test_input_validation(self):
        cases = {
            'extra_field': {'secret': SECRET, 'repo': 'x'},
            'missing_secret': {},
            'bad_name': {'secret': 'Bad_Name'},
            'reuses_github_secret': {'secret': self.plain['recovery']['secrets']['github']},
            'reuses_main_secret': {'secret': self.plain['secrets']['bot']},
            'not_a_dict': ['x'],
        }
        for name, value in cases.items():
            v = deepcopy(self.plain)
            v['recovery']['ci_relay'] = value
            with self.subTest(case=name), self.assertRaises(ValueError):
                candidate(v)
        for key in ('ci', 'relay', 'ci_relays'):  # unknown recovery keys stay rejected
            v = deepcopy(self.v)
            v['recovery'][key] = {'secret': SECRET}
            with self.subTest(key=key), self.assertRaises(ValueError):
                candidate(v)
        v = deepcopy(self.v)
        del v['recovery']
        v['ci_relay'] = {'secret': SECRET}
        with self.assertRaises(ValueError):  # only inside the recovery section
            candidate(v)


if __name__ == '__main__':
    unittest.main()
