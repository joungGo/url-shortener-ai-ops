"""WF10 input tool (WF11 follow-up, 2026-11-27). Offline: fake kubectl, renderer validation."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_ops import wf10_inputs as w
from ai_ops.wf10_deployment import candidate
import test_wf10_deployment as base
import test_wf10_recovery_render as render

API = ['10.0.1.11', '10.0.2.12']
K8S_SVC, PROM_SVC, PROM_POD = '172.20.0.1', '172.20.33.44', '10.0.3.55'
IMAGE = w.REGISTRY + '@sha256:' + 'a' * 64


def lab_config(plain=False):
    config = {k: deepcopy(x) for k, x in base.fixture()['config'].items() if k in w.LAB_KEYS}
    if plain:
        for conn in config['runtime.json']['connections'].values():
            if conn['kind'] == 'prometheus':
                conn.update(base_url='http://kube-prometheus-stack-prometheus.monitoring.svc:9090',
                            ca_file=None, token_file=None)
    return config


def cluster(plain=False, endpoints=None, pod_ip=PROM_POD):
    """Fake kubectl responses keyed by the READS name."""
    return {'lab_config': {'data': {k: json.dumps(x) for k, x in lab_config(plain).items()}},
            'endpoints': {'items': [{'endpoints': [{'addresses': [ip]} for ip in (API if endpoints is None else endpoints)]}]},
            'kubernetes_service': {'spec': {'clusterIP': K8S_SVC}},
            'prometheus_service': {'spec': {'clusterIP': PROM_SVC}},
            'prometheus_pod': {'status': {'podIP': pod_ip}}}


def facts(**kw):
    responses = cluster(**kw)
    return w.collect(lambda name: deepcopy(responses[name]))


class InputsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {'AI_OPS_FILES_ROOT': str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.binding = render.recovery_section()['normal_binding']

    def build(self, f=None, **kw):
        args = {'bot_user_id': 'UBOT0001', 'alert_channel_id': 'CALERT01', 'image': IMAGE,
                'source_revision': 'b' * 40, 'normal_binding': self.binding, **kw}
        return w.build(f or facts(plain=True), **args)

    def test_collect_runs_only_the_five_fixed_get_commands(self):
        responses, commands = cluster(plain=True), []

        def run(command, **kw):
            commands.append(command)
            name = next(k for k, args in w.READS.items() if command[-len(args):] == args)
            return subprocess.CompletedProcess(command, 0, json.dumps(responses[name]).encode(), b'')

        with patch.object(w.subprocess, 'run', side_effect=run):
            got = w.collect(w.kubectl_reader(Path('/k/config')))
        self.assertEqual(len(commands), 5)
        self.assertEqual(sorted(c[4:] for c in commands), sorted(w.READS.values()))
        for c in commands:
            self.assertEqual(c[:4], ['kubectl', '--request-timeout=25s', '--kubeconfig', '/k/config'])
            self.assertEqual((c[4], c[6], c[-2:]), ('-n', 'get', ['-o', 'json']))  # -n <ns> get ... -o json
            self.assertFalse({'secret', 'secrets', 'apply', 'create', 'delete', 'patch', 'edit', 'exec'} & set(c))
        self.assertEqual(got['api_endpoints'], API)
        self.assertEqual((got['kubernetes_service_ip'], got['prometheus_service_ip'], got['prometheus_pod_ip']),
                         (K8S_SVC, PROM_SVC, PROM_POD))
        self.assertEqual(got['lab_config'], lab_config(plain=True))
        self.assertNotIn('runbook-policy.json', got['lab_config'])
        with self.assertRaisesRegex(ValueError, 'wf10_inputs_read_rejected'):
            w.kubectl_reader()('secrets')
        with patch.object(w.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, b'', b'x')), \
                self.assertRaisesRegex(ValueError, 'wf10_inputs_read_failed'):
            w.kubectl_reader()('lab_config')

    def test_collect_rejects_missing_or_unusable_addresses(self):
        with self.assertRaisesRegex(ValueError, 'wf10_inputs_endpoints_missing'):
            facts(endpoints=[])
        for bad in ('127.0.0.1', '169.254.1.1', '0.0.0.0', 'fd00::1', 'not-an-ip'):
            with self.subTest(ip=bad), self.assertRaises(ValueError):
                facts(pod_ip=bad)
            with self.subTest(endpoint=bad), self.assertRaises(ValueError):
                facts(endpoints=[bad])

    def test_collector_cidrs_are_endpoints_both_service_ips_and_the_prometheus_pod(self):
        value, _ = self.build()
        expected = sorted(ip + '/32' for ip in API + [K8S_SVC, PROM_SVC, PROM_POD])
        self.assertEqual(value['network']['collector_cidrs'], expected)
        for ip in (K8S_SVC, PROM_SVC):  # pre-DNAT matching: both Service IPs (2026-11-26 install)
            self.assertIn(ip + '/32', value['network']['collector_cidrs'])
        f = facts(plain=True, endpoints=[API[0], API[0], K8S_SVC])  # duplicates collapse
        self.assertEqual(w.collector_cidrs(f), sorted({API[0] + '/32', K8S_SVC + '/32', PROM_SVC + '/32', PROM_POD + '/32'}))

    def test_build_passes_the_renderer_with_relay_and_publication_on_by_default(self):
        value, summary = self.build()
        self.assertEqual(summary['status'], 'candidate_rendered_not_approved')
        self.assertIn(['Deployment', 'ai-ops-egress'], summary['resources'])
        rendered = candidate(value)  # the summary is the real renderer result for the written inputs
        self.assertEqual(summary['manifest_sha256'], rendered['manifest_sha256'])
        self.assertEqual(summary['resources'], [[r['kind'], r['metadata']['name']] for r in rendered['resources']])
        self.assertEqual((value['namespace'], value['name'], value['service_account'], value['pvc_name']),
                         ('ai-ops', 'ai-ops', 'ai-ops', 'ai-ops-data'))
        r = value['recovery']
        self.assertEqual((r['ci_relay'], r['publish']), ({'secret': 'ai-ops-ci-github'}, {'secret': 'ai-ops-pr-writer'}))
        self.assertEqual(r['principals'], {'U0ABBAD4FT7': 149856960})
        self.assertEqual((r['approvers'], r['author_id']), (['U0ABBAD4FT7'], 354870297))
        self.assertEqual(r['approval']['observation_seconds'], 30)
        self.assertEqual(r['normal_binding'], self.binding)
        self.assertEqual(value['source']['cluster_id'], value['config']['bindings.json']['bindings'][0]['target']['cluster_id'])
        policy = value['config']['runbook-policy.json']['targets'][0]
        self.assertEqual((policy['alias'], policy['target']), (value['config']['bindings.json']['bindings'][0]['alias'],
                                                               value['config']['bindings.json']['bindings'][0]['target']))
        self.assertEqual(value['slack'], {'team_id': 'T0AAZ9N5GT1', 'app_id': 'A0CJHH8ULUC', 'bot_user_id': 'UBOT0001',
                                          'user_id': 'U0ABBAD4FT7', 'investigation_channel_id': 'C0CK82SD8NL',
                                          'alert_channel_id': 'CALERT01'})
        off, _ = self.build(ci_relay=False, publish=False)
        self.assertNotIn('ci_relay', off['recovery'])
        self.assertNotIn('publish', off['recovery'])
        only, _ = self.build(publish=False)
        self.assertEqual((only['recovery'].get('ci_relay'), only['recovery'].get('publish')), ({'secret': 'ai-ops-ci-github'}, None))

    def test_secrets_follow_the_prometheus_scheme_and_never_repeat(self):
        plain, _ = self.build(facts(plain=True))
        self.assertNotIn('collector', plain['secrets'])  # no credential over plain HTTP
        tls, _ = self.build(facts(plain=False))
        self.assertEqual(tls['secrets']['collector'], 'ai-ops-collector')
        for value in (plain, tls):
            names = (list(value['secrets'].values()) + list(value['recovery']['secrets'].values())
                     + [value['recovery']['ci_relay']['secret'], value['recovery']['publish']['secret']])
            self.assertEqual(len(names), len(set(names)), names)
        self.assertEqual(len(set(w.SECRETS.values()) | set(w.RECOVERY_SECRETS.values()) | {w.CI_RELAY_SECRET, w.PUBLISH_SECRET}), 9)

    def test_operator_inputs_are_checked_before_rendering(self):
        # The tool's own checks fire first (exact codes), even where the renderer would also reject.
        cases = {'bad_bot': ({'bot_user_id': 'ubot'}, 'slack_id'), 'bad_channel': ({'alert_channel_id': 'C'}, 'slack_id'),
                 'trailing_bot': ({'bot_user_id': 'UBOT0001;x'}, 'slack_id'),
                 'trailing_override': ({'overrides': {'team_id': 'T0AAZ9N5GT1\n'}}, 'slack_id'),
                 'bad_override_id': ({'overrides': {'team_id': 't-1'}}, 'slack_id'),
                 'alert_is_investigation': ({'alert_channel_id': 'C0CK82SD8NL'}, 'slack_boundary'),
                 'bot_is_user': ({'bot_user_id': 'U0ABBAD4FT7'}, 'slack_boundary'),
                 'author_is_approver': ({'overrides': {'author_github_id': 149856960}}, 'author_must_differ_from_approver'),
                 'unknown_override': ({'overrides': {'merge': True}}, 'override'),
                 'other_registry': ({'image': 'example.invalid/ai-ops@sha256:' + 'a' * 64}, 'image'),
                 'tag_not_digest': ({'image': w.REGISTRY + ':latest'}, 'image'),
                 'trailing_image': ({'image': IMAGE + '0'}, 'image'),
                 'short_revision': ({'source_revision': 'b' * 39}, 'image'),
                 'long_revision': ({'source_revision': 'b' * 41}, 'image')}
        for name, (kw, code) in cases.items():
            with self.subTest(case=name), self.assertRaisesRegex(ValueError, f'^wf10_inputs_{code}_rejected$|^wf10_inputs_{code}$'):
                self.build(**kw)
        with self.assertRaises(ValueError):  # left to the renderer: alert channel must be C/G
            self.build(alert_channel_id='DALERT01')
        for name, change in {'kind': {'kind': 'wf10_candidate_inputs'}, 'version': {'schema_version': 2}}.items():
            with self.subTest(facts=name), self.assertRaisesRegex(ValueError, 'wf10_inputs_facts_rejected'):
                self.build({**facts(plain=True), **change})
        value, _ = self.build(overrides={'team_id': 'TOTHER01', 'investigation_channel_id': 'COTHER01', 'user_id': 'UOTHER01'})
        self.assertEqual((value['slack']['team_id'], value['slack']['investigation_channel_id']), ('TOTHER01', 'COTHER01'))
        self.assertEqual((value['recovery']['approvers'], value['recovery']['principals']), (['UOTHER01'], {'UOTHER01': 149856960}))

    def test_image_comes_from_the_scanned_import_proof(self):
        proof = {'registry_digest': 'sha256:' + 'c' * 64, 'source_revision': 'd' * 40, 'scan_findings': 0}
        self.assertEqual(w.image_from_proof(proof), (w.REGISTRY + '@sha256:' + 'c' * 64, 'd' * 40))
        for name, change in {'findings': {'scan_findings': 1}, 'unscanned': {'scan_findings': None},
                             'bad_digest': {'registry_digest': 'c' * 64}, 'bad_revision': {'source_revision': 'main'},
                             'missing': {'registry_digest': None}}.items():
            with self.subTest(case=name), self.assertRaisesRegex(ValueError, 'wf10_inputs_image_proof_rejected'):
                w.image_from_proof({**proof, **change})

    def test_cli_writes_new_private_files_and_prints_only_a_summary(self):
        responses = cluster(plain=True)
        facts_path, out = self.root / 'facts.json', self.root / 'inputs.json'
        stdout = io.StringIO()
        with patch.object(w, 'kubectl_reader', return_value=lambda name: deepcopy(responses[name])), redirect_stdout(stdout):
            self.assertEqual(w.main(['collect', '--output', str(facts_path)]), 0)
            self.assertEqual(w.main(['collect', '--output', str(facts_path)]), 1)  # never overwrite
        lines = [json.loads(x) for x in stdout.getvalue().splitlines()]
        self.assertEqual(lines[0]['status'], 'collected')
        self.assertEqual(lines[1], {'status': 'wf10_inputs_rejected', 'action': 'collect'})
        self.assertEqual(facts_path.stat().st_mode & 0o777, 0o600)
        proof, prepared = self.root / 'proof.json', self.root / 'prepared.json'
        proof.write_text(json.dumps({'registry_digest': 'sha256:' + 'a' * 64, 'source_revision': 'b' * 40, 'scan_findings': 0}))
        prepared.write_text(json.dumps({'schema_version': 1, 'binding': self.binding}))
        args = ['build', '--facts', str(facts_path), '--normal-binding', str(prepared), '--image-proof', str(proof),
                '--bot-user-id', 'UBOT0001', '--alert-channel-id', 'CALERT01', '--output', str(out)]
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(w.main(args + ['--team-id', 'TOTHER01']), 0)
            self.assertEqual(w.main(args), 1)  # existing output
        first, second = [json.loads(x) for x in stdout.getvalue().splitlines()]
        self.assertEqual(set(first), {'status', 'manifest_sha256', 'resources'})
        self.assertEqual(second, {'status': 'wf10_inputs_rejected', 'action': 'build'})
        written = json.loads(out.read_text())
        self.assertEqual((written['image'], written['slack']['team_id']), (IMAGE, 'TOTHER01'))
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        out.unlink()
        prepared.write_text(json.dumps(self.binding))  # a bare binding is not a prepare_b6_normal_ci result
        with redirect_stdout(io.StringIO()):
            self.assertEqual(w.main(args), 1)
        self.assertFalse(out.exists())


if __name__ == '__main__':
    unittest.main()
