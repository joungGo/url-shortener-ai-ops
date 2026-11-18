"""Slack worker 설정을 조사 접수에 사용하되 실행 권한은 복사하지 않는다."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_ops.b_contracts import load_b_settings
from ai_ops.c2_active_capture import build_active_capture_job
from ai_ops.c_capture_config import COMMON_MATERIALIZE
from ai_ops.cli import main
from ai_ops.job_queue import JobQueue
from ai_ops.runtime_settings import runbook_settings_kwargs
from ai_ops.worker import Worker
from test_c2_live_job import IMAGE, worker_deployment
from test_job_queue import API, Clock, collect_snapshot, ROOT


def materialized_worker():
    deployment = worker_deployment()
    pod = deployment['spec']['template']['spec']
    pod['volumes'][0] = {'name': 'config', 'emptyDir': {'medium': 'Memory', 'sizeLimit': '2Mi'}}
    pod['volumes'].append({'name': 'common-projected', 'projected': {'defaultMode': 420, 'sources': [
        {'configMap': {'name': 'ci-config-slack-current'}}, {'configMap': {'name': 'ci-normal-binding'}}]}})
    pod['containers'][0].update(volumeMounts=[{'name': 'config', 'mountPath': '/etc/ai-ops', 'readOnly': True}],
                                envFrom=[{'secretRef': {'name': 'worker-only-secret'}}])
    pod['initContainers'] = [{'name': 'common-materialize', 'image': IMAGE,
        'command': ['python', '-c', COMMON_MATERIALIZE],
        'volumeMounts': [{'name': 'common-projected', 'mountPath': '/projected/common', 'readOnly': True},
                         {'name': 'config', 'mountPath': '/out/common'}],
        'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']},
                            'readOnlyRootFilesystem': True},
        'resources': {'requests': {'cpu': '10m', 'memory': '16Mi'},
                      'limits': {'cpu': '100m', 'memory': '64Mi'}},
        'imagePullPolicy': 'IfNotPresent', 'terminationMessagePath': '/dev/termination-log',
        'terminationMessagePolicy': 'File'}]
    return deployment


def build(deployment):
    return build_active_capture_job(deployment, trial_id=123, failure_revision='b' * 40)


class CaptureConfigurationTest(unittest.TestCase):
    def test_materialized_layout_keeps_deadlines_identity_and_minimal_permissions(self):
        original = materialized_worker(); before = deepcopy(original)
        job = build(original); pod = job['spec']['template']['spec']
        self.assertEqual(original, before)
        self.assertEqual(job['spec']['activeDeadlineSeconds'], 180)
        self.assertEqual(job['spec']['ttlSecondsAfterFinished'], 300)
        self.assertEqual(job['spec']['backoffLimit'], 0)
        self.assertEqual(job['metadata']['annotations']['ai-ops/worker-deployment-uid'], 'worker-uid')
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertFalse(pod['enableServiceLinks'])
        self.assertEqual(pod['securityContext']['runAsUser'], 10001)
        self.assertEqual([v['name'] for v in pod['volumes']], ['config', 'data', 'common-projected', 'tmp'])
        self.assertEqual(pod['containers'][0]['image'], IMAGE)
        self.assertEqual(pod['initContainers'][0]['image'], IMAGE)
        self.assertEqual(pod['containers'][0]['env'], [{'name': 'HOME', 'value': '/tmp'}])
        serialized = json.dumps(job)
        for forbidden in ('secretRef', 'secretName', 'serviceAccountToken', 'hostPath', 'worker-only-secret'):
            self.assertNotIn(forbidden, serialized)
        args = pod['containers'][0]['args']
        self.assertEqual(args[args.index('--runbook-policy') + 1], '/etc/ai-ops/runbook-policy.json')
        self.assertEqual(args[args.index('--request-id') + 1], 'c2-source-capture-123')
        self.assertEqual(args[args.index('--target') + 1], 'url-shortener-b6-lab')

    def test_legacy_configmap_and_projection_remain_supported(self):
        for config in ({'name': 'config', 'configMap': {'name': 'ci-config', 'defaultMode': 420}},
                       worker_deployment()['spec']['template']['spec']['volumes'][0]):
            dep = worker_deployment(); dep['spec']['template']['spec']['volumes'][0] = config
            pod = build(dep)['spec']['template']['spec']
            self.assertNotIn('initContainers', pod)
            self.assertNotIn('--runbook-policy', pod['containers'][0]['args'])
            self.assertEqual([v['name'] for v in pod['volumes']], ['config', 'data', 'tmp'])

    def test_unknown_or_credential_bearing_materializers_are_rejected(self):
        changes = [
            lambda p: p.pop('initContainers'),
            lambda p: p['initContainers'].append(deepcopy(p['initContainers'][0])),
            lambda p: p['initContainers'][0].update(command=['sh', '-c', 'echo unexpected']),
            lambda p: p['initContainers'][0].update(image=IMAGE[:-1] + 'b'),
            lambda p: p['initContainers'][0].update(env=[{'name': 'TOKEN', 'value': 'do-not-use'}]),
            lambda p: p['initContainers'][0].update(envFrom=[{'secretRef': {'name': 'other'}}]),
            lambda p: p['initContainers'][0].update(args=['extra']),
            lambda p: p['initContainers'][0].update(restartPolicy='Always'),
            lambda p: p['initContainers'][0]['securityContext'].update(privileged=True),
            lambda p: p['initContainers'][0]['resources']['limits'].update(memory='1Gi'),
            lambda p: p['initContainers'][0]['volumeMounts'].append({'name': 'identity', 'mountPath': '/identity'}),
            lambda p: p['containers'][0]['volumeMounts'][0].update(readOnly=False),
            lambda p: p['volumes'][0]['emptyDir'].pop('sizeLimit'),
            lambda p: p['volumes'][0]['emptyDir'].update(medium=''),
            lambda p: p['volumes'][0].update(hostPath={'path': '/etc'}),
            lambda p: p['volumes'].append(deepcopy(p['volumes'][0])),
            lambda p: p['volumes'][1].update(secret={'secretName': 'unexpected'}),
            lambda p: p['volumes'][-1]['projected']['sources'].append({'secret': {'name': 'other'}}),
            lambda p: p['volumes'][-1]['projected']['sources'][0].update(serviceAccountToken={'path': 'token'}),
            lambda p: p['volumes'][-1]['projected']['sources'][0]['configMap'].update(name='arbitrary'),
            lambda p: p['volumes'][-1]['projected']['sources'][0]['configMap'].update(optional=True),
            lambda p: p['volumes'][-1]['projected']['sources'][0]['configMap'].update(items=[{'key': 'a', 'path': 'bindings.json'}]),
            lambda p: p['volumes'][-1]['projected'].update(defaultMode=511),
            lambda p: p['volumes'][-1]['projected']['sources'].__setitem__(1, deepcopy(p['volumes'][-1]['projected']['sources'][0])),
        ]
        for index, mutate in enumerate(changes):
            dep = materialized_worker(); mutate(dep['spec']['template']['spec'])
            with self.subTest(case=index), self.assertRaises(ValueError):
                build(dep)

    def test_legacy_layout_cannot_hide_an_initializer_or_secret_projection(self):
        for mutate in (lambda p: p.update(initContainers=materialized_worker()['spec']['template']['spec']['initContainers']),
                       lambda p: p['volumes'][0]['projected']['sources'].append({'secret': {'name': 'other'}})):
            dep = worker_deployment(); mutate(dep['spec']['template']['spec'])
            with self.assertRaises(ValueError): build(dep)


class CaptureConfigQueueTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='capture-config-', dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / 'projected'; self.source.mkdir()
        self.output = self.root / 'common'; self.output.mkdir()
        env = patch.dict(os.environ, {'AI_OPS_FILES_ROOT': str(self.root)})
        env.start(); self.addCleanup(env.stop)
        for source, name in [('targets.practice.example.json', 'bindings.json'),
                             ('b-runtime.example.json', 'runtime.json'), ('metric-queries.json', 'metric-queries.json')]:
            shutil.copyfile(ROOT / 'config' / source, self.source / name)
        binding = json.loads((self.source/'bindings.json').read_text())
        binding['bindings'][0]['alias'] = 'url-shortener-b6-lab'
        (self.source/'bindings.json').write_text(json.dumps(binding))
        settings = load_b_settings(self.source/'bindings.json', self.source/'runtime.json', self.source/'metric-queries.json')
        policy = {'schema_version': 1,
            'registry_sha256': hashlib.sha256((ROOT/'runbooks/index.yaml').read_bytes()).hexdigest(),
            'metadata_sha256': hashlib.sha256((ROOT/'runbooks/search-metadata.json').read_bytes()).hexdigest(),
            'targets': [{'alias': settings.bindings.bindings[0].alias,
                         'target': settings.bindings.bindings[0].target.model_dump(mode='json'),
                         'document_ids': ['cache']}]}
        (self.source/'runbook-policy.json').write_text(json.dumps(policy))
        self.pod = build(materialized_worker())['spec']['template']['spec']

    def materialize(self):
        script = self.pod['initContainers'][0]['command'][2]
        script = script.replace("'/projected/common'", repr(str(self.source))).replace("'/out/common'", repr(str(self.output)))
        return subprocess.run([sys.executable, '-c', script], capture_output=True, timeout=5)

    def args(self):
        return [value.replace('/etc/ai-ops/', str(self.output) + '/').replace('/data/incidents', str(self.root/'store'))
                for value in self.pod['containers'][0]['args']]

    def test_materialized_settings_match_worker_and_queue_is_processed_without_network(self):
        # Kubernetes projection symlinks are followed; hidden revision entries are skipped.
        revision = self.source/'..version'; revision.mkdir()
        (self.source/'runtime.json').rename(revision/'runtime.json')
        (self.source/'runtime.json').symlink_to('..version/runtime.json')
        self.assertEqual(self.materialize().returncode, 0)
        self.assertEqual({p.name for p in self.output.iterdir()},
                         {'bindings.json', 'runtime.json', 'metric-queries.json', 'runbook-policy.json'})
        for path in self.output.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(path.is_symlink())
        stream = io.StringIO()
        with redirect_stdout(stream): self.assertEqual(main(self.args()), 0)
        queued = json.loads(stream.getvalue())
        settings = load_b_settings(self.output/'bindings.json', self.output/'runtime.json',
            self.output/'metric-queries.json', **runbook_settings_kwargs(self.output))
        without_policy = load_b_settings(self.output/'bindings.json', self.output/'runtime.json', self.output/'metric-queries.json')
        self.assertNotEqual(settings.config_hash, without_policy.config_hash)
        queue = JobQueue(self.root/'store')
        with queue.transaction() as db:
            payload = json.loads(db.execute('SELECT payload FROM jobs').fetchone()[0])
            self.assertEqual(payload['config_hash'], settings.config_hash)
        clock = Clock(); api = API(settings, clock); api.docs['deployment']['spec']['replicas'] = 1
        snapshot = collect_snapshot(settings, settings.bindings.bindings[0].target, transport=api, clock=clock)
        with patch('socket.create_connection', side_effect=AssertionError('network_forbidden')), \
             patch.object(queue.engine, 'load_api_key', side_effect=AssertionError('model_forbidden')):
            result = Worker(queue, settings, collector=lambda *a, **kw: snapshot).run_once()
        self.assertEqual(result['job_id'], queued['job_id'])
        self.assertEqual(result['state'], 'partial')

    def test_missing_or_invalid_policy_never_submits_a_job(self):
        self.assertEqual(self.materialize().returncode, 0)
        policy = self.output/'runbook-policy.json'
        for value in (None, '{}'):
            if value is None: policy.unlink()
            else: policy.write_text(value)
            with self.subTest(value=value), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                try: result = main(self.args())
                except SystemExit as exc: result = exc.code
                self.assertNotEqual(result, 0)
            queue = JobQueue(self.root/'store')
            with queue.transaction() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_materializer_enforces_existing_file_size_limit(self):
        (self.source/'bindings.json').write_bytes(b'x' * 65537)
        self.assertNotEqual(self.materialize().returncode, 0)
        self.assertFalse((self.output/'bindings.json').exists())


if __name__ == '__main__':
    unittest.main()
