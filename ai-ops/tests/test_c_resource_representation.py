"""proxy와 kubectl 간 원본 형식 차이·실제 변경의 거절·값 없는 진단을 검증한다."""

from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_ops.c_approval import ApprovalRejected
from ai_ops.c_recovery import _encoded_values
from ai_ops.c_recovery_snapshot import (RecoveryResourcesChanged,
                                       require_same_recovery_resources, same_recovery_resources)
from ai_ops.c_trial_read_budget import resource_change_diagnostics
from ai_ops.c_trial_source import TrialSource
from ai_ops.c_trial_transport import C4TrialTransport, SnapshotChanged


MANAGED_FIELDS = [{'manager': 'fixture-controller', 'operation': 'Update',
    'apiVersion': 'coordination.k8s.io/v1', 'fieldsType': 'FieldsV1', 'fieldsV1': {'f:spec': {}}}]


class ResourceRepresentationTest(unittest.TestCase):
    def setUp(self):
        from test_c_trial_execution import TrialExecutionTest
        self.fixture = f = TrialExecutionTest(); f.setUp(); self.addCleanup(f.doCleanups)
        record = f.executor.inject(f.authorization)  # 전부 인메모리 대역, 외부 쓰기 없음.
        for key in ('application', 'deployment'):
            f.fixture.snapshot[key]['metadata']['managedFields'] = deepcopy(MANAGED_FIELDS)
        f.fixture.server.value['metadata']['managedFields'] = deepcopy(MANAGED_FIELDS)
        self.source = TrialSource(SimpleNamespace(kubeconfig='/tmp/fixture-only', normal_binding=None,
            operator_state_root=None, recovery_repository='joungGo/c03-execution-lab'),
            record, f.transport, clock=lambda: f.fixture.at, git_reader=Mock(), history_reader=Mock())
        self.source.audit._github_values = Mock(return_value=_encoded_values('sha256:'+'0'*64).encode())
        self.commands = []
        self.force_legacy = False

    def run_command(self, *command):
        self.commands.append(command)
        self.assertEqual(command[0], 'kubectl')
        resource = {'application': 'application', 'deployment': 'deployment', 'lease': 'lease'}[command[6]]
        value = deepcopy(self.fixture.transport.snapshot()[resource])
        if self.force_legacy or '--show-managed-fields=true' not in command:
            value['metadata'].pop('managedFields', None)
        return json.dumps(value)

    def collect(self):
        with patch.object(self.source.audit, '_run', side_effect=self.run_command):
            return self.source.collect()

    def test_unchanged_resources_pass_through_real_trial_source_and_audit_adapter(self):
        _, raw = self.collect()
        self.assertEqual(len(self.commands), 6)
        for resource in ('application', 'deployment', 'lease'):
            self.assertEqual(raw[resource], self.fixture.transport.snapshot()[resource])
            self.assertEqual(raw[resource]['metadata']['managedFields'], MANAGED_FIELDS)
        self.assertTrue(all('--show-managed-fields=true' in c for c in self.commands))

    def test_legacy_printer_reproduces_the_same_trial_source_failure_without_resource_writes(self):
        self.force_legacy = True
        original = self.fixture.transport.snapshot
        def legacy_mixed_snapshot():
            snapshot = original()
            # 기존 실행기: app/dep는 kubectl, Lease만 새 proxy로 조회한다.
            for resource in ('application', 'deployment'):
                snapshot[resource]['metadata'].pop('managedFields')
            return snapshot
        self.fixture.transport.snapshot = legacy_mixed_snapshot
        writes = len(self.fixture.transport.calls)
        before = self.fixture.transport.snapshot()
        with self.assertRaises(RecoveryResourcesChanged) as error:
            self.collect()
        self.assertEqual(error.exception.phase, 'trial_source.before_to_raw')
        self.assertEqual(error.exception.changed_paths, ('/lease/metadata/managedFields',))
        self.assertEqual(self.fixture.transport.snapshot(), before)
        self.assertEqual(len(self.fixture.transport.calls), writes)

    def test_transport_get_explicitly_preserves_all_resource_management_fields(self):
        t = C4TrialTransport.__new__(C4TrialTransport)
        obj = self.fixture.transport.snapshot()['lease']
        t._kube = Mock(return_value=json.dumps(obj).encode())
        for key in ('application', 'deployment', 'lease'):
            self.assertEqual(t.get(key), obj)
            self.assertIn('--show-managed-fields=true', t._kube.call_args.args)

    def test_management_fields_actual_change_and_other_ownership_changes_still_fail(self):
        before = self.fixture.transport.snapshot()
        for resource, section, field in [('application', 'metadata', 'managedFields'),
                ('deployment', 'metadata', 'managedFields'), ('lease', 'metadata', 'managedFields'),
                ('lease', 'metadata', 'uid'), ('lease', 'metadata', 'resourceVersion'),
                ('lease', 'spec', 'holderIdentity'), ('lease', 'spec', 'renewTime'),
                ('application', 'spec', 'source')]:
            after = deepcopy(before)
            after[resource][section][field] = 'synthetic-private-value'
            with self.subTest(resource=resource, field=field):
                self.assertFalse(same_recovery_resources(before, after))
                with self.assertRaises(RecoveryResourcesChanged) as error:
                    require_same_recovery_resources(before, after, phase='trial_source.raw_to_after')
                self.assertIn('/'+resource+'/'+section+'/'+field, error.exception.changed_paths)
                self.assertNotIn('synthetic-private-value', str(error.exception.diagnostic()))

    def test_only_fixed_paths_survive_nested_errors_unknown_keys_or_cycles(self):
        before = self.fixture.transport.snapshot(); after = deepcopy(before)
        after['lease']['metadata']['annotations']['private-key'] = 'private-value'
        after['lease']['metadata']['unknown-private-key'] = 'private-value'
        cause = RecoveryResourcesChanged('trial_source.before_to_raw', before, after)
        error = ApprovalRejected('fresh_live_proof_unavailable'); error.__cause__ = cause
        cause.__cause__ = error
        records = resource_change_diagnostics(error)
        self.assertEqual(records, [{'phase': 'trial_source.before_to_raw', 'changed_paths': [
            '/lease/metadata/annotations', '/lease/metadata/other'], 'truncated': False}])
        self.assertNotIn('private', json.dumps(records))
        self.assertEqual(resource_change_diagnostics(ValueError('private-value')), [])
        with self.assertRaisesRegex(ValueError, 'phase_rejected'):
            RecoveryResourcesChanged('private-value', before, after)

    def test_snapshot_retry_exception_retains_safe_comparison_cause(self):
        before = self.fixture.transport.snapshot(); after = deepcopy(before)
        after['lease']['metadata']['resourceVersion'] = 'private-resource-version'
        t = C4TrialTransport.__new__(C4TrialTransport); t.clock = lambda: self.fixture.fixture.at
        versions = {key: iter([before[key], after[key]]) for key in ('application', 'deployment', 'lease')}
        t.get = lambda key: {'items': []} if key in ('pods', 'replicasets') else next(versions[key])
        with self.assertRaises(SnapshotChanged) as error:
            t.snapshot()
        self.assertEqual(resource_change_diagnostics(error.exception)[0]['changed_paths'],
                         ['/lease/metadata/resourceVersion'])


if __name__ == '__main__':
    unittest.main()
