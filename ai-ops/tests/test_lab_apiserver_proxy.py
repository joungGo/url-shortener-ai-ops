"""lab NetworkPolicy: API 서버 pods/proxy → lab-api:8000 허용 (WF11 2026-11-26).

실제 실행에서 복구 정상 판정의 ``/readyz``·``/items`` 읽기가 API 서버 Pod proxy로
lab-api에 닿지 못하고 시간 초과됐다(``lab-deny`` + Prometheus만 허용). 정상 판정이
``unknown``이면 S2a의 ``already_recovered``가 나올 수 없다. 입력으로 검증된 API 서버
endpoint ``/32``에서 오는 8000번만 추가로 연다. 다른 lab Pod와 포트는 그대로 닫혀 있다.
"""
import copy
from unittest import TestCase

from ai_ops import recovery_observe
from ai_ops.b6_lab import LabInputs, build_lab, validate_lab
from test_b6_lab import inputs

PROMETHEUS = {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'monitoring'}},
              'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'prometheus'}}}


def policy(bundle, name):
    return next(i for i in bundle['items'] if i['kind'] == 'NetworkPolicy' and i['metadata']['name'] == name)['spec']


class LabApiServerProxyTest(TestCase):
    def setUp(self):
        self.cidrs = ['10.0.10.176/32', '10.0.11.105/32', '172.20.0.1/32']
        self.inputs = inputs().model_copy(update={'api_server_cidrs': self.cidrs})
        self.bundle = build_lab(LabInputs.model_validate(self.inputs.model_dump()))

    def test_lab_api_ingress_is_prometheus_and_api_server_on_8000_only(self):
        spec = policy(self.bundle, 'lab-api')
        self.assertEqual(spec['ingress'], [
            {'from': [PROMETHEUS], 'ports': [{'port': 8000}]},
            {'from': [{'ipBlock': {'cidr': c}} for c in self.cidrs], 'ports': [{'port': 8000}]}])
        for rule in spec['ingress']:
            for peer in rule['from']:
                if 'ipBlock' in peer:
                    self.assertTrue(peer['ipBlock']['cidr'].endswith('/32'))
                    self.assertNotIn('except', peer['ipBlock'])

    def test_other_lab_pods_stay_closed_to_the_api_server(self):
        database = policy(self.bundle, 'lab-database')
        self.assertEqual(database['ingress'], [{'from': [{'podSelector': {'matchLabels': {'app': 'lab-api'}}}],
                                                'ports': [{'port': 5432}, {'port': 6379}]}])
        self.assertEqual(policy(self.bundle, 'lab-deny'),
                         {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress'], 'ingress': [], 'egress': []})
        api_rules = [r for n in ('lab-database', 'lab-dns', 'lab-collector')
                     for r in policy(self.bundle, n).get('ingress', [])]
        self.assertFalse(any('ipBlock' in p for r in api_rules for p in r['from']))

    def test_opened_port_is_the_port_the_healthy_judgment_reads(self):
        api = next(i for i in self.bundle['items'] if i['kind'] == 'Deployment' and i['metadata']['name'] == 'lab-api')
        [container] = api['spec']['template']['spec']['containers']
        self.assertEqual([p['containerPort'] for p in container['ports']], [recovery_observe.HTTP_PORT])
        self.assertEqual(recovery_observe.HTTP_PORT, 8000)
        self.assertIn(':8000/proxy/', recovery_observe.PROXY.replace('\\', ''))

    def test_validation_rejects_a_widened_or_moved_rule(self):
        self.assertEqual(validate_lab(self.bundle)['namespace'], 'ai-ops-lab')
        cases = {
            'any_source': lambda s: s['ingress'][1]['from'].append({'ipBlock': {'cidr': '0.0.0.0/0'}}),
            'other_port': lambda s: s['ingress'][1].update(ports=[{'port': 8001}]),
            'no_port_limit': lambda s: s['ingress'][1].pop('ports'),
            'extra_cidr': lambda s: s['ingress'][1]['from'].append({'ipBlock': {'cidr': '10.9.9.9/32'}}),
        }
        for name, change in cases.items():
            bundle = copy.deepcopy(self.bundle)
            change(policy(bundle, 'lab-api'))
            with self.subTest(case=name), self.assertRaises(ValueError):
                validate_lab(bundle)

    def test_inputs_still_reject_ranges(self):
        for bad in (['10.0.0.0/16'], ['0.0.0.0/0']):
            with self.subTest(cidrs=bad), self.assertRaises(ValueError):
                LabInputs.model_validate({**self.inputs.model_dump(), 'api_server_cidrs': bad})
