"""Offline v1.35 manifest schema validation (vendored OpenAPI subset, no cluster)."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_ops import k8s_schema
from ai_ops.k8s_schema import SchemaError, document, validate_manifest
from ai_ops.wf10_deployment import candidate
from test_wf10_deployment import fixture

ROOT = Path(__file__).resolve().parents[2]


class SchemaSubsetTest(unittest.TestCase):
    def test_subset_provenance_and_closure(self):
        doc = document()
        self.assertEqual(doc['source']['tag'], 'v1.35.8')
        self.assertEqual(len(doc['source']['sha256']), 3)
        schemas = doc['schemas']

        def refs(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == '$ref':
                        yield value
                    else:
                        yield from refs(value)
            elif isinstance(node, list):
                for value in node:
                    yield from refs(value)
        missing = {r for s in schemas.values() for r in refs(s)} - set(schemas)
        self.assertEqual(missing, set())
        for name in doc['roots']:
            self.assertTrue(schemas[name]['x-kubernetes-group-version-kind'])
        self.assertNotIn('description', json.dumps(schemas))

    def test_builder_pins_same_sources_as_vendored_file(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('builder', ROOT / 'ai-ops/scripts/build_k8s_schema_subset.py')
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        self.assertEqual(builder.SOURCES, document()['source']['sha256'])
        self.assertEqual(builder.OUTPUT, k8s_schema.DATA)
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'api__v1_openapi.json').write_text('{}')
            for name in list(builder.SOURCES)[1:]:
                Path(tmp, name).write_text('{}')
            with self.assertRaisesRegex(SystemExit, 'hash mismatch'):
                builder.build(tmp)


class ManifestValidationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {'AI_OPS_FILES_ROOT': tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.resources = candidate(fixture())['resources']

    def kind(self, kind, suffix=''):
        return deepcopy(next(r for r in self.resources if r['kind'] == kind
                             and r['metadata']['name'] == 'wf10-fixture' + suffix))

    def test_all_rendered_resources_are_valid(self):
        self.assertEqual(len(self.resources), 8)
        for resource in self.resources:
            validate_manifest(resource)

    def test_shape_errors_are_rejected_with_path_only(self):
        def mutate(kind, change, suffix=''):
            obj = self.kind(kind, suffix)
            change(obj)
            return obj
        pod = lambda d: d['spec']['template']['spec']
        cases = {
            'unknown pod field': mutate('Deployment', lambda d: pod(d)['securityContext'].update(fsGroupChangePolicyy='OnRootMismatch')),
            'unknown top-level field': mutate('Deployment', lambda d: d.update(specs={})),
            'string replicas': mutate('Deployment', lambda d: d['spec'].update(replicas='1')),
            'bool replicas': mutate('Deployment', lambda d: d['spec'].update(replicas=True)),
            'missing container name': mutate('Deployment', lambda d: pod(d)['containers'][0].pop('name')),
            'missing selector': mutate('Deployment', lambda d: d['spec'].pop('selector')),
            'int32 overflow port': mutate('Service', lambda d: d['spec']['ports'][0].update(port=2**31), '-events'),
            'string service port': mutate('Service', lambda d: d['spec']['ports'][0].update(port='9443'), '-events'),
            'non-string configmap value': mutate('ConfigMap', lambda d: d['data'].update(x=1), '-config'),
            'unknown ipBlock field': mutate('NetworkPolicy', lambda d: d['spec']['egress'][-1]['to'][0]['ipBlock'].update(exclude=[]), '-sender'),
            'wrong apiVersion': mutate('Deployment', lambda d: d.update(apiVersion='v1')),
            'unsupported kind': {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'x'}},
        }
        for label, obj in cases.items():
            with self.subTest(label):
                with self.assertRaises(SchemaError) as error:
                    validate_manifest(obj)
                self.assertNotIn('OnRootMismatch', str(error.exception))

    def test_int_or_string_and_quantity_unions(self):
        policy = self.kind('NetworkPolicy')
        policy['spec']['ingress'][0]['ports'][0]['port'] = 'https'  # IntOrString accepts a name
        validate_manifest(policy)
        policy['spec']['ingress'][0]['ports'][0]['port'] = 1.5
        with self.assertRaises(SchemaError):
            validate_manifest(policy)
        deployment = self.kind('Deployment')
        deployment['spec']['template']['spec']['containers'][0]['resources']['limits']['cpu'] = 2  # Quantity number
        validate_manifest(deployment)
        deployment['spec']['template']['spec']['containers'][0]['resources']['limits']['cpu'] = ['2']
        with self.assertRaises(SchemaError):
            validate_manifest(deployment)

    def test_renderer_refuses_to_emit_schema_invalid_manifest(self):
        with patch('ai_ops.wf10_deployment.INIT_RESOURCES', {'requests': {'cpu': '10m'}, 'limitz': {}}):
            with self.assertRaisesRegex(ValueError, 'wf10_schema_rejected'):
                candidate(fixture())
        result = candidate(fixture())
        self.assertEqual(result['schema_validation'],
                         {'kubernetes': 'v1.35.8', 'mode': 'offline_openapi_subset_strict', 'server_dry_run': False})
        self.assertIn('server_side_dry_run', result['required_live_gates'])


if __name__ == '__main__':
    unittest.main()
