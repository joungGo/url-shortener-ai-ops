"""WF10 renderer: the cluster Prometheus over in-cluster plain HTTP without credentials.

User decision 2026-11-26 (WF11 install): kube-prometheus-stack serves
http://kube-prometheus-stack-prometheus.monitoring.svc:9090 without auth (2026-11-18, B6).
The collector may use it only with no token/CA; then no collector secret is required
or mounted. HTTPS + collector secret remains the other accepted shape. Offline.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_wf10_deployment as base
from ai_ops.wf10_deployment import candidate
from test_wf10_recovery_render import recovery_section
from test_wf10_restart_render import restart_section

URL = 'http://kube-prometheus-stack-prometheus.monitoring.svc:9090'


def plain(value):
    v = deepcopy(value)
    for conn in v['config']['runtime.json']['connections'].values():
        if conn['kind'] == 'prometheus':
            conn.update(base_url=URL, token_file=None, ca_file=None)
    del v['secrets']['collector']
    return v


def prometheus(v):
    return next(c for c in v['config']['runtime.json']['connections'].values() if c['kind'] == 'prometheus')


class PlainPrometheusTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {'AI_OPS_FILES_ROOT': tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.v = plain(base.fixture())

    def main_pod(self, result):
        return next(r for r in result['resources'] if r['kind'] == 'Deployment'
                    and r['metadata']['name'] == 'wf10-fixture')['spec']['template']['spec']

    def test_renders_without_a_collector_secret_or_mount(self):
        result = candidate(self.v)
        pod = self.main_pod(result)
        everything = json.dumps(result['resources'])
        self.assertNotIn('/run/collector', everything)
        self.assertNotIn('collector-source', everything)
        self.assertFalse({'collector', 'collector-source'} & {x['name'] for x in pod['volumes']})
        worker = next(c for c in pod['containers'] if c['name'] == 'worker')
        self.assertEqual([m['mountPath'] for m in worker['volumeMounts'] if m['mountPath'].startswith('/run')], [])
        # The rendered runtime config keeps the credential-free connection.
        runtime = next(r for r in result['resources'] if r['kind'] == 'ConfigMap'
                       and 'runtime.json' in r.get('data', {}))['data']['runtime.json']
        conn = prometheus({'config': {'runtime.json': json.loads(runtime)}})
        self.assertEqual((conn['base_url'], conn.get('token_file'), conn.get('ca_file')), (URL, None, None))
        policy = next(r for r in result['resources'] if r['kind'] == 'NetworkPolicy'
                      and r['metadata']['name'] == 'wf10-fixture')
        collector_rule = next(e for e in policy['spec']['egress']
                              if any('ipBlock' in t for t in e['to']))
        self.assertEqual([t['ipBlock']['cidr'] for t in collector_rule['to']], ['10.0.0.1/32', '10.0.0.2/32'])
        self.assertIn({'protocol': 'TCP', 'port': 9090}, collector_rule['ports'])
        self.assertTrue(any('Prometheus Pod IP' in x for x in result['external_prerequisites']))
        self.assertTrue(any('plain HTTP without authentication' in x for x in result['residual_risks']))

    def test_rejects_credentials_or_other_ports_over_http(self):
        cases = {
            'token': lambda v: prometheus(v).update(token_file='/run/collector/token'),
            'ca': lambda v: prometheus(v).update(ca_file='/run/collector/ca.crt'),
            'port_80': lambda v: prometheus(v).update(base_url='http://kube-prometheus-stack-prometheus.monitoring.svc'),
            'port_8080': lambda v: prometheus(v).update(base_url='http://kube-prometheus-stack-prometheus.monitoring.svc:8080'),
            'collector_secret_kept': lambda v: v['secrets'].update(collector='fixture-collector'),
            'kubernetes_http': lambda v: next(c for c in v['config']['runtime.json']['connections'].values()
                                              if c['kind'] == 'kubernetes').update(base_url='http://kubernetes.default.svc:9090',
                                                                                   token_file=None, ca_file=None),
        }
        for name, change in cases.items():
            v = deepcopy(self.v)
            change(v)
            with self.subTest(case=name), self.assertRaises(ValueError):
                candidate(v)

    def test_https_still_requires_the_collector_secret(self):
        v = base.fixture()
        del v['secrets']['collector']
        with self.assertRaisesRegex(ValueError, 'wf10_fields_rejected'):
            candidate(v)
        pod = self.main_pod(candidate(base.fixture()))
        worker = next(c for c in pod['containers'] if c['name'] == 'worker')
        self.assertIn('/run/collector', [m['mountPath'] for m in worker['volumeMounts']])

    def test_recovery_renders_but_restart_needs_tls(self):
        v = {**self.v, 'recovery': recovery_section()}
        result = candidate(v)
        self.assertTrue(any(r['kind'] == 'PrometheusRule' for r in result['resources']))
        # The IRSA webhook mutates every Pod using the service account (all three share it).
        accounts = {r['spec']['template']['spec']['serviceAccountName'] for r in result['resources']
                    if r['kind'] == 'Deployment'}
        self.assertEqual(accounts, {v['service_account']})
        self.assertIn('IRSA environment is injected into every container of every Pod using the service account '
                      '(main, sender, egress), not only the relay', result['residual_risks'])
        v['recovery']['restart'] = restart_section()
        with self.assertRaisesRegex(ValueError, 'wf10_restart_prometheus_tls_required'):
            candidate(v)

    def test_credential_separation_counts_the_present_secrets(self):
        v = {**self.v, 'recovery': recovery_section()}
        v['recovery']['secrets']['reader'] = v['secrets']['bot']
        with self.assertRaisesRegex(ValueError, 'credential_separation'):
            candidate(v)


if __name__ == '__main__':
    unittest.main()
