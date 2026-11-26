"""WF10 renderer: no container reads a ConfigMap/Secret volume directly (WF11 2026-11-26).

First live start of WF10: every process exited at startup because Kubernetes
ConfigMap volume files are symlinks (``key -> ..data/key``) and the runtime
readers open configuration with O_NOFOLLOW (``Symbolic link loop:
/etc/ai-ops/runbook-policy.json``). The renderer now copies ConfigMap keys like
the Secrets: an init container writes regular uid10001/0600 files into a memory
emptyDir. Offline; the copy script itself is executed against a symlinked tree.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_wf10_recovery_render as render
from ai_ops.runbook_search import _read
from ai_ops.wf10_deployment import candidate

# The worker's projected ServiceAccount token/CA is read with a reader that
# explicitly allows projected-volume links (deployment_check / collectors).
ALLOWED_DIRECT = {'data', 'kubernetes'}


class ConfigCopyTest(unittest.TestCase):
    def setUp(self):
        self.r = render.RecoveryRenderTest()
        self.r.setUp()
        self.addCleanup(self.r.doCleanups)
        self.result = candidate(self.r.v)
        self.pods = {d['metadata']['name']: d['spec']['template']['spec']
                     for d in self.result['resources'] if d['kind'] == 'Deployment'}
        self.config = next(r for r in self.result['resources']
                           if r['kind'] == 'ConfigMap' and r['metadata']['name'] == 'wf10-fixture-config')

    def test_no_container_mounts_a_configmap_or_secret_volume(self):
        for name, pod in self.pods.items():
            volumes = {v['name']: v for v in pod['volumes']}
            for container in pod['containers']:
                for m in container['volumeMounts']:
                    volume = volumes[m['name']]
                    with self.subTest(pod=name, container=container['name'], volume=m['name']):
                        if m['name'] in ALLOWED_DIRECT:
                            continue
                        self.assertEqual(set(volume) - {'name'}, {'emptyDir'})
            # Every *-source volume is read only by init copies, never by the main containers.
            sources = {v['name'] for v in pod['volumes'] if v['name'].endswith('-source')}
            mounted = {m['name'] for c in pod['containers'] for m in c['volumeMounts']}
            self.assertFalse(sources & mounted, name)

    def test_main_pod_config_copy_covers_every_configmap_key(self):
        pod = self.pods['wf10-fixture']
        source = next(v for v in pod['volumes'] if v['name'] == 'config-source')['configMap']
        keys = sorted(self.config['data'])
        self.assertEqual(source['name'], 'wf10-fixture-config')
        self.assertEqual(source['items'], [{'key': k, 'path': k} for k in keys])
        self.assertIn('runbook-policy.json', keys)
        copy = next(i for i in pod['initContainers'] if i['name'] == 'copy-config')
        self.assertEqual({m['mountPath']: m['name'] for m in copy['volumeMounts']},
                         {'/source': 'config-source', '/out': 'config'})
        for container in pod['containers']:
            mounts = {m['mountPath']: m for m in container['volumeMounts']}
            self.assertEqual((mounts['/etc/ai-ops']['name'], mounts['/etc/ai-ops']['readOnly']), ('config', True))
        sender = self.pods['wf10-fixture-sender']
        [policy] = [m for m in sender['containers'][0]['volumeMounts'] if m['mountPath'] == '/etc/ai-ops']
        self.assertEqual(policy['name'], 'policy')

    def test_copy_script_turns_configmap_symlinks_into_files_the_strict_reader_accepts(self):
        copy = next(i for i in self.pods['wf10-fixture']['initContainers'] if i['name'] == 'copy-config')
        self.assertEqual(copy['command'][:2], ['python', '-c'])
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source, out = root / 'source', root / 'out'
            data = source / '..2026_11_26_00_00_00.000000000'
            data.mkdir(parents=True)
            out.mkdir()
            for key, value in self.config['data'].items():
                (data / key).write_text(value)
            (source / '..data').symlink_to(data.name)
            for key in self.config['data']:
                (source / key).symlink_to('..data/' + key)
            # The failure seen live: the strict reader refuses the ConfigMap link.
            with self.assertRaises(OSError):
                _read(source, 'runbook-policy.json')
            script = copy['command'][2].replace('/source/', str(source) + '/').replace('/out/', str(out) + '/')
            subprocess.run([sys.executable, '-c', script], check=True)
            for key, value in self.config['data'].items():
                path = out / key
                self.assertFalse(path.is_symlink())
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
                self.assertEqual(_read(out, key).decode(), value)


if __name__ == '__main__':
    unittest.main()
