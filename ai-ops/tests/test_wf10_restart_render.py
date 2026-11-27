"""WF10 renderer: optional recovery.restart section (offline; no cluster or credentials)."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_ops.recovery_approval_wiring import RecoveryApprovalConfig
from ai_ops.wf10_deployment import RESTART_EXECUTOR, build, candidate
import test_wf10_deployment as base
from test_wf10_recovery_render import recovery_section


def restart_section():
    return {'approval': {'wait_seconds': 3600, 'approval_seconds': 600, 'observation_seconds': 30,
                         'revalidation_seconds': 3},
            'execution_seconds': 10, 'prometheus_secret': 'fixture-recovery-prometheus',
            'prior_ready_query': 'max_over_time(kube_pod_status_ready{pod_template_hash="$TEMPLATE_HASH"}[1d])',
            'dependency_queries': ['min(up{job="redis"})']}


class RestartRenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {'AI_OPS_FILES_ROOT': str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.plain = {**base.fixture(), 'recovery': recovery_section()}
        self.v = deepcopy(self.plain)
        self.v['recovery']['restart'] = restart_section()

    def by(self, result, kind, suffix=''):
        return next(r for r in result['resources']
                    if r['kind'] == kind and r['metadata']['name'] == 'wf10-fixture' + suffix)

    def config(self, result):
        return RecoveryApprovalConfig.model_validate_json(self.by(result, 'ConfigMap', '-config')['data']['recovery-approval.json'])

    def mounts(self, result):
        pod = self.by(result, 'Deployment')['spec']['template']['spec']
        return pod, {c['name']: {m['mountPath'] for m in c['volumeMounts']} for c in pod['containers']}

    def test_absent_restart_keeps_the_recovery_candidate(self):
        result = candidate(self.plain)
        self.assertNotIn('restart', result['recovery'])
        self.assertIsNone(self.config(result).restart)
        self.assertEqual(candidate({**self.plain, 'recovery': {**self.plain['recovery'], 'restart': None}}), result)
        self.assertNotIn('recovery-prometheus', json.dumps(result))
        self.assertFalse(any('horizontalpodautoscalers' in x for x in result['external_prerequisites']))
        # The healthy check (PR67) needs pods/proxy on the reader identity in every recovery candidate.
        self.assertTrue(any('pods/proxy get' in x for x in result['external_prerequisites']))

    def test_present_restart_renders_the_validated_section_with_execution_disabled(self):
        result = candidate(self.v)
        restart = self.config(result).restart
        prometheus = next(c for c in self.v['config']['runtime.json']['connections'].values()
                          if c['kind'] == 'prometheus')
        self.assertEqual((restart.executor_id, restart.execution_seconds, restart.approval.scope,
                          restart.approval.approvers, restart.approval.version),
                         (RESTART_EXECUTOR, 10, 'execute_command', ['UUSER01'], 1))
        self.assertEqual((restart.prometheus.api_url, restart.prometheus.token_file, restart.prometheus.ca_file),
                         (prometheus['base_url'], '/run/recovery-prometheus/token', '/run/recovery-prometheus/ca.crt'))
        self.assertEqual(restart.prometheus.dependency_queries, ['min(up{job="redis"})'])
        self.assertEqual(result['recovery']['restart'], 'execution_disabled')
        self.assertFalse(result['execution_allowed'])
        prerequisites = ' '.join(result['external_prerequisites'])
        for needed in ('events list', 'horizontalpodautoscalers list', 'read-only Prometheus token', 'pods/proxy get'):
            self.assertIn(needed, prerequisites)
        self.assertNotIn('patch', ' '.join(x for x in result['external_prerequisites'] if 'restart path' in x))

    def test_only_the_relay_gets_the_prometheus_secret_and_network_is_unchanged(self):
        result = candidate(self.v)
        pod, mounts = self.mounts(result)
        self.assertIn('/run/recovery-prometheus', mounts['slack-relay'])
        for role in ('worker', 'event-receiver', 'slack-ingress'):
            self.assertNotIn('/run/recovery-prometheus', mounts[role], role)
        sources = {v['secret']['secretName'] for v in pod['volumes'] if 'secret' in v}
        self.assertIn('fixture-recovery-prometheus', sources)
        plain = candidate(self.plain)
        for suffix in ('', '-sender', '-egress'):
            self.assertEqual(self.by(result, 'NetworkPolicy', suffix), self.by(plain, 'NetworkPolicy', suffix))

    def test_rendered_config_loads_and_builds_the_read_only_restart_wiring(self):
        from ai_ops import slack_server
        data = self.by(candidate(self.v), 'ConfigMap', '-config')['data']
        directory = self.root / 'etc'
        directory.mkdir()
        for name in ('bindings.json', 'runtime.json', 'metric-queries.json', 'runbook-policy.json',
                     'slack-policy.json', 'recovery-approval.json'):
            (directory / name).write_text(data[name])
        recovery = slack_server.load_recovery(directory)
        self.assertEqual(recovery.restart.approval.approvers, recovery.prepare_proposal.approvers)
        policy = json.loads(data['slack-policy.json'])
        self.assertTrue(set(recovery.restart.approval.approvers) <= {g['user_id'] for g in policy['grants']})

    def test_invalid_restart_sections_are_rejected_without_writing(self):
        cases = {
            'not_a_dict': lambda r: r.update(restart=['x']),
            'missing_field': lambda r: r['restart'].pop('prometheus_secret'),
            'extra_field': lambda r: r['restart'].update(executor_id='local-restart-other'),
            'approval_extra': lambda r: r['restart']['approval'].update(approvers=['UOTHER01']),
            'secret_is_reader': lambda r: r['restart'].update(prometheus_secret='fixture-recovery-reader'),
            'secret_is_collector': lambda r: r['restart'].update(prometheus_secret='fixture-collector'),
            'secret_bad_name': lambda r: r['restart'].update(prometheus_secret='Bad Name'),
            'relay_budget': lambda r: r['restart'].update(execution_seconds=13),
            'execution_zero': lambda r: r['restart'].update(execution_seconds=0),
            'query_not_text': lambda r: r['restart'].update(dependency_queries=[1]),
            'too_many_queries': lambda r: r['restart'].update(dependency_queries=['up'] * 9),
            'empty_prior_query': lambda r: r['restart'].update(prior_ready_query=''),
        }
        for name, change in cases.items():
            v = deepcopy(self.v)
            change(v['recovery'])
            out = self.root / ('out-' + name)
            with self.subTest(case=name), self.assertRaises((ValueError, TypeError)):
                build(v, out)
            self.assertFalse(out.exists())

    def test_package_round_trip_stays_within_the_limit(self):
        from ai_ops.wf10_change_plan import plan
        out = self.root / 'candidate'
        build(self.v, out)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ['candidate.json', 'inputs.json'])
        self.assertEqual(json.loads((out / 'candidate.json').read_text())['recovery']['restart'], 'execution_disabled')
        order = [(s['kind'], s['name'].removeprefix('wf10-fixture')) for s in plan(out)['apply']]
        self.assertEqual(order[-2:], [('Deployment', '-egress'), ('Deployment', '')])


if __name__ == '__main__':
    unittest.main()
