"""WF11 pre-install edge/identity manifests: names must match the WF10 renderer output."""
from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
import unittest

import test_wf10_recovery_render as render
from ai_ops.wf10_deployment import candidate
from ai_ops.wf11_edge import main, manifests

ROLE = 'arn:aws:iam::764428911194:role/urlshortener-practice-recovery-ecr-read'
HOST = 'slack-ops.bidservice.store'
WRITE_VERBS = {'create', 'update', 'patch', 'delete', 'deletecollection', 'escalate', 'bind', 'impersonate', '*'}


class EdgeTest(unittest.TestCase):
    def setUp(self):
        self.r = render.RecoveryRenderTest()
        self.r.setUp()
        self.addCleanup(self.r.doCleanups)
        self.v = self.r.v
        self.items = manifests(self.v, role_arn=ROLE, slack_host=HOST)
        self.rendered = candidate(self.v)['resources']

    def item(self, kind, name=None):
        found = [i for i in self.items if i['kind'] == kind and (name is None or i['metadata']['name'] == name)]
        self.assertEqual(len(found), 1, (kind, name))
        return found[0]

    def deployment(self):
        return next(r for r in self.rendered if r['kind'] == 'Deployment' and r['metadata']['name'] == 'wf10-fixture')

    def test_names_match_the_renderer(self):
        pod = self.deployment()['spec']['template']['spec']
        sa = self.item('ServiceAccount', self.v['service_account'])
        self.assertEqual(pod['serviceAccountName'], sa['metadata']['name'])
        self.assertEqual(sa['metadata']['annotations'], {'eks.amazonaws.com/role-arn': ROLE})
        self.assertFalse(sa['automountServiceAccountToken'])
        volumes = {x['secret']['secretName'] for x in pod['volumes'] if 'secret' in x}
        reader = self.item('Secret')
        self.assertEqual(reader['type'], 'kubernetes.io/service-account-token')
        self.assertIn(reader['metadata']['name'], volumes)
        self.assertEqual(reader['metadata']['name'], self.v['recovery']['secrets']['reader'])
        certificate = self.item('Certificate')
        self.assertIn(certificate['spec']['secretName'], volumes)
        self.assertEqual(certificate['spec']['secretName'], self.v['secrets']['slack_tls'])
        self.assertEqual(certificate['spec']['dnsNames'], [HOST])
        route = self.item('IngressRouteTCP')
        [target] = route['spec']['routes'][0]['services']
        service = next(r for r in self.rendered if r['kind'] == 'Service' and r['metadata']['name'] == target['name'])
        self.assertEqual([p['port'] for p in service['spec']['ports']], [target['port']])
        self.assertEqual(route['spec']['tls'], {'passthrough': True})
        self.assertEqual(route['spec']['routes'][0]['match'], f'HostSNI(`{HOST}`)')
        for i in self.items:
            if i['kind'] in ('ServiceAccount', 'Secret', 'Certificate', 'IngressRouteTCP'):
                self.assertEqual(i['metadata']['namespace'], self.v['namespace'])

    def test_reader_is_read_only_and_bound_to_its_own_service_account(self):
        reader_sa = self.item('ServiceAccount', 'wf10-fixture-recovery-reader')
        self.assertNotIn('annotations', reader_sa['metadata'])  # no IRSA on the reader
        self.assertEqual(self.item('Secret')['metadata']['annotations'],
                         {'kubernetes.io/service-account.name': 'wf10-fixture-recovery-reader'})
        roles = [i for i in self.items if i['kind'] == 'Role']
        self.assertEqual(sorted(r['metadata']['namespace'] for r in roles), ['ai-ops-lab', 'argocd'])
        for role in roles:
            for rule in role['rules']:
                self.assertFalse(set(rule['verbs']) & WRITE_VERBS, rule)
                self.assertNotIn('*', rule['resources'] + rule['apiGroups'])
                self.assertNotIn('secrets', rule['resources'])
        lab = next(r for r in roles if r['metadata']['namespace'] == 'ai-ops-lab')
        self.assertEqual({(tuple(r['resources']), tuple(r['verbs']), tuple(r.get('resourceNames', []))) for r in lab['rules']},
                         {(('deployments',), ('get',), ('lab-api',)), (('replicasets',), ('list',), ()),
                          (('pods',), ('list',), ()), (('pods/proxy',), ('get',), ()),
                          (('leases',), ('get',), ('b6-scenario-lock',))})
        argo = next(r for r in roles if r['metadata']['namespace'] == 'argocd')
        self.assertEqual(argo['rules'], [{'apiGroups': ['argoproj.io'], 'resources': ['applications'],
                                          'resourceNames': ['lab-target'], 'verbs': ['get']}])
        for binding in (i for i in self.items if i['kind'] == 'RoleBinding'):
            self.assertEqual(binding['subjects'], [{'kind': 'ServiceAccount', 'name': 'wf10-fixture-recovery-reader',
                                                    'namespace': self.v['namespace']}])
            self.assertEqual(binding['roleRef']['kind'], 'Role')

    def test_rejects_bad_install_facts_and_missing_recovery(self):
        for kwargs in ({'role_arn': 'arn:aws:iam::764428911194:user/x', 'slack_host': HOST},
                       {'role_arn': ROLE + ' ', 'slack_host': HOST},
                       {'role_arn': ROLE, 'slack_host': 'Slack.Example.com'},
                       {'role_arn': ROLE, 'slack_host': 'localhost'},
                       {'role_arn': ROLE, 'slack_host': 'x.example.com`) || HostSNI(`*'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                manifests(self.v, **kwargs)
        plain = {k: x for k, x in self.v.items() if k != 'recovery'}
        with self.assertRaisesRegex(ValueError, 'recovery_section_required'):
            manifests(plain, role_arn=ROLE, slack_host=HOST)
        broken = deepcopy(self.v)
        broken['recovery']['secrets']['reader'] = 'Bad_Name'
        with self.assertRaises(ValueError):
            manifests(broken, role_arn=ROLE, slack_host=HOST)

    def test_cli_writes_once_and_fails_with_a_fixed_code(self):
        source = self.r.root / 'inputs.json'
        source.write_text(json.dumps(self.v))
        out = self.r.root / 'edge.json'
        args = ['--inputs', str(source), '--role-arn', ROLE, '--slack-host', HOST, '--output', str(out)]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 0)
        written = json.loads(out.read_text())
        self.assertEqual((written['kind'], len(written['items'])), ('List', 9))
        before = out.read_bytes()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(main(args), 1)  # exists: never overwritten
        self.assertEqual(out.read_bytes(), before)
        self.assertEqual(stdout.getvalue().strip(), '{"status": "wf11_edge_rejected"}')


if __name__ == '__main__':
    unittest.main()
