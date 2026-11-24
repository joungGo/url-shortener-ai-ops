"""WF10 renderer with the optional WF07 recovery section (offline; no cluster or credentials)."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_ops.recovery_approval_wiring import RecoveryApprovalConfig
from ai_ops.recovery_egress_proxy import PORT
from ai_ops.wf10_deployment import NON_PUBLIC, build, candidate
from ai_ops.wf10_preparation import digest, encoded
import test_wf10_deployment as base


def recovery_section():
    import test_c_recovery_proof as proof
    p = proof.RecoveryProofTest()
    p.setUp()
    return {'approvers': ['UUSER01'],
            'approval': {'wait_seconds': 3600, 'approval_seconds': 600, 'observation_seconds': 30,
                         'revalidation_seconds': 10},
            'principals': {'UUSER01': 1234567}, 'author_id': 7654321, 'aws_role_name': 'ai-ops-recovery-reader',
            'secrets': {'reader': 'fixture-recovery-reader', 'github': 'fixture-github-read'},
            'normal_binding': p.binding.model_dump(mode='json')}


class RecoveryRenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {'AI_OPS_FILES_ROOT': str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.v = {**base.fixture(), 'recovery': recovery_section()}

    def render(self, v=None):
        return candidate(v or self.v)

    def by(self, kind, suffix=''):
        return next(r for r in self.render()['resources']
                    if r['kind'] == kind and r['metadata']['name'] == 'wf10-fixture' + suffix)

    def test_absent_section_keeps_the_previous_candidate(self):
        plain = candidate(base.fixture())
        self.assertNotIn('recovery', plain)
        self.assertEqual(candidate({**base.fixture(), 'recovery': None}), plain)
        self.assertNotIn('-egress', json.dumps(plain))

    def test_present_section_adds_config_mounts_and_egress_proxy_only(self):
        result = self.render()
        self.assertEqual([(r['kind'], r['metadata']['name']) for r in result['resources']], [
            ('ConfigMap', 'wf10-fixture-config'), ('Deployment', 'wf10-fixture'), ('Service', 'wf10-fixture-events'),
            ('Service', 'wf10-fixture-slack'), ('NetworkPolicy', 'wf10-fixture'), ('Deployment', 'wf10-fixture-sender'),
            ('Service', 'wf10-fixture-sender'), ('NetworkPolicy', 'wf10-fixture-sender'),
            ('Deployment', 'wf10-fixture-egress'), ('Service', 'wf10-fixture-egress'),
            ('NetworkPolicy', 'wf10-fixture-egress')])
        self.assertEqual(result['recovery'], {'enabled': True, 'publication': 'not_wired',
                                              'egress_proxy': f'http://wf10-fixture-egress.ai-ci.svc:{PORT}'})
        self.assertFalse(result['execution_allowed'])
        self.assertTrue(any('ecr:DescribeImages' in x for x in result['external_prerequisites']))
        data = self.by('ConfigMap', '-config')['data']
        config = RecoveryApprovalConfig.model_validate_json(data['recovery-approval.json'])
        self.assertEqual((config.observe.api_url, config.prepare.egress_proxy, config.prepare.drafts_dir),
                         ('https://kubernetes.default.svc', f'http://wf10-fixture-egress.ai-ci.svc:{PORT}',
                          '/data/recovery-drafts'))
        self.assertEqual(json.loads(data['normal-ci-binding.json']), self.v['recovery']['normal_binding'])

    def test_only_the_relay_gets_reader_and_github_secrets(self):
        pod = self.by('Deployment')['spec']['template']['spec']
        mounts = {c['name']: {m['mountPath'] for m in c['volumeMounts']} for c in pod['containers']}
        self.assertTrue({'/run/recovery-reader', '/run/ai-github'} <= mounts['slack-relay'])
        for role in ('worker', 'event-receiver', 'slack-ingress'):
            self.assertFalse({'/run/recovery-reader', '/run/ai-github'} & mounts[role], role)
        sources = {v['secret']['secretName'] for v in pod['volumes'] if 'secret' in v}
        self.assertTrue({'fixture-recovery-reader', 'fixture-github-read'} <= sources)

    def test_egress_pod_holds_nothing_and_policies_pin_the_path(self):
        pod = self.by('Deployment', '-egress')['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual([c['command'] for c in pod['containers']], [['python', '-m', 'ai_ops.recovery_egress_proxy']])
        self.assertFalse(any('secret' in v or 'persistentVolumeClaim' in v or 'projected' in v for v in pod['volumes']))
        main = self.by('NetworkPolicy')['spec']
        to_proxy = [rule for rule in main['egress'] if rule['ports'] == [{'protocol': 'TCP', 'port': PORT}]]
        self.assertEqual(to_proxy[0]['to'], [{'podSelector': {'matchLabels': {
            'app.kubernetes.io/name': 'wf10-fixture-egress', 'app.kubernetes.io/part-of': 'wf10'}}}])
        self.assertFalse(any('ipBlock' in peer and peer['ipBlock']['cidr'] == '0.0.0.0/0'
                             for rule in main['egress'] for peer in rule['to']))
        proxy = self.by('NetworkPolicy', '-egress')['spec']
        self.assertEqual(proxy['ingress'], [{'from': [{'podSelector': {'matchLabels': {
            'app.kubernetes.io/name': 'wf10-fixture', 'app.kubernetes.io/part-of': 'wf10'}}}],
            'ports': [{'protocol': 'TCP', 'port': PORT}]}])
        self.assertEqual(proxy['egress'][1], {'to': [{'ipBlock': {'cidr': '0.0.0.0/0', 'except': NON_PUBLIC}}],
                                              'ports': [{'protocol': 'TCP', 'port': 443}]})

    def test_rendered_config_starts_the_relay_with_a_live_bridge_offline(self):
        from ai_ops import slack_server
        data = self.by('ConfigMap', '-config')['data']
        directory = self.root / 'etc'
        directory.mkdir()
        for name in ('bindings.json', 'runtime.json', 'metric-queries.json', 'runbook-policy.json',
                     'slack-policy.json', 'recovery-approval.json'):
            (directory / name).write_text(data[name])
        recovery = slack_server.load_recovery(directory)
        self.assertIsNotNone(recovery)
        self.assertEqual(set(recovery.prepare.principals), set(recovery.prepare_proposal.approvers))

    def test_invalid_sections_are_rejected_without_writing(self):
        cases = {
            'approver_not_granted': lambda r: r.update(approvers=['UOTHER01'], principals={'UOTHER01': 1234567}),
            'missing_field': lambda r: r.pop('aws_role_name'),
            'extra_field': lambda r: r.update(extra=True),
            'secret_reused': lambda r: r['secrets'].update(github='fixture-collector'),
            'same_two_secrets': lambda r: r['secrets'].update(github='fixture-recovery-reader'),
            'revalidation_too_long': lambda r: r['approval'].update(revalidation_seconds=16),
            'principal_missing': lambda r: r.update(principals={'UOTHER01': 1}),
            'author_is_reviewer': lambda r: r.update(author_id=1234567),
            'bad_binding': lambda r: r['normal_binding'].update(cluster_arn='arn:aws:eks:x'),
            'bad_role': lambda r: r.update(aws_role_name='bad role'),
        }
        for name, change in cases.items():
            v = deepcopy(self.v)
            change(v['recovery'])
            out = self.root / ('out-' + name)
            with self.subTest(case=name), self.assertRaises(ValueError):
                build(v, out)
            self.assertFalse(out.exists())

    def test_change_plan_orders_egress_before_the_main_pod(self):
        from ai_ops.wf10_change_plan import plan
        directory = self.root / 'candidate'
        build(self.v, directory)
        document = plan(directory)
        order = [(s['kind'], s['name'].removeprefix('wf10-fixture')) for s in document['apply']]
        self.assertEqual(order[-2:], [('Deployment', '-egress'), ('Deployment', '')])
        self.assertLess(order.index(('NetworkPolicy', '-egress')), order.index(('Deployment', '-egress')))
        self.assertIn('main_pod_internet_egress_only_via_proxy', document['post_checks'])
        self.assertEqual(len(document['rollback']['steps']), 11)
        plain = self.root / 'plain'
        build(base.fixture(), plain)
        self.assertNotIn('main_pod_internet_egress_only_via_proxy', plan(plain)['post_checks'])

    def test_package_round_trip_and_tamper(self):
        from ai_ops.wf10_deployment import verify
        directory = self.root / 'pkg'
        build(self.v, directory)
        self.assertEqual(verify(directory)['status'], 'candidate_verified_not_approved')
        inputs = json.loads((directory / 'inputs.json').read_text())
        inputs['recovery']['author_id'] = 1
        (directory / 'inputs.json').write_text(json.dumps(inputs))
        with self.assertRaises(ValueError):
            verify(directory)
