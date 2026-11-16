"""GET만 수행해 관측한 controller time 갱신과 실제 변경의 차단을 재현한다."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from ai_ops.c_approval import ApprovalRejected
from ai_ops.c_recovery_snapshot import same_application_observation, same_recovery_resources
import test_c_observation_stability as stability


def controller_entry(time):
    # 정상 상태의 실제 관측에서 같은 항목의 time만 reconciledAt와 함께 변했다.
    return {'manager': 'argocd-application-controller', 'operation': 'Update',
        'apiVersion': 'argoproj.io/v1alpha1', 'fieldsType': 'FieldsV1', 'time': time,
        'fieldsV1': {'f:status': {'f:reconciledAt': {}, 'f:health': {'f:status': {}}}}}


class ControllerObservationTest(unittest.TestCase):
    def setUp(self):
        f = stability.SnapshotComparisonTest(); f.setUp()
        self.now = f.now
        self.before, self.after = f.before, f.after
        for s in (self.before, self.after):
            app = s['application']; app.update(apiVersion='argoproj.io/v1alpha1', kind='Application')
            app['metadata']['managedFields'] = [
                {'manager': 'fixture-declarative-client', 'operation': 'Apply', 'time': '2026-11-01T00:00:00Z'},
                controller_entry(app['status']['reconciledAt'])]
        self.after['application']['metadata']['generation'] = 2

    def same(self):
        return same_recovery_resources(self.before, self.after, now=self.now)

    def test_normal_controller_time_refresh_preserves_full_originals(self):
        before, after = deepcopy(self.before), deepcopy(self.after)
        self.assertTrue(self.same())
        self.assertEqual(self.before, before); self.assertEqual(self.after, after)
        self.assertTrue(same_application_observation(before['application'], after['application'], now=self.now))

    def test_only_one_existing_controller_entry_may_change(self):
        for change in ('added', 'removed', 'reordered', 'duplicate', 'other_time'):
            self.setUp(); entries = self.after['application']['metadata']['managedFields']
            if change == 'added': entries.append({'manager': 'other'})
            elif change == 'removed': entries.pop(0)
            elif change == 'reordered': entries.reverse()
            elif change == 'duplicate':
                for s in [self.before, self.after]:
                    s['application']['metadata']['managedFields'].append(deepcopy(self.before['application']['metadata']['managedFields'][1]))
            else: entries[0]['time'] = entries[1]['time']
            with self.subTest(change=change): self.assertFalse(self.same())

    def test_controller_identity_operation_version_type_and_ownership_are_preserved(self):
        cases = [('manager', 'other'), ('operation', 'Apply'), ('apiVersion', 'argoproj.io/v1beta1'),
                 ('fieldsType', 'Other'), ('fieldsV1', {'f:spec': {}}), ('subresource', 'status')]
        for field, value in cases:
            self.setUp()
            self.after['application']['metadata']['managedFields'][1][field] = value
            with self.subTest(field=field): self.assertFalse(self.same())
        # 양쪽이 같은 미등록 controller/소유권이어도 time 예외로 승격하지 않는다.
        for field, value in cases:
            self.setUp()
            for s in [self.before, self.after]:s['application']['metadata']['managedFields'][1][field] = value
            with self.subTest(both=field):self.assertFalse(self.same())

    def test_management_times_must_match_both_exact_observation_times(self):
        for side, value in [('before', '2026-11-11T00:00:00Z'), ('after', '2026-11-11T00:00:00Z'),
                            ('after', 'not-time'), ('after', None), ('after', 123),
                            ('after', '2026-11-12T00:00:01'), ('after', '2026-11-12T00:00:01Z')]:
            self.setUp(); target=self.before if side=='before' else self.after
            target['application']['metadata']['managedFields'][1]['time']=value
            with self.subTest(side=side, value=value):self.assertFalse(self.same())
        for side in ('before','after'):
            self.setUp();target=self.before if side=='before' else self.after
            del target['application']['metadata']['managedFields'][1]['time']
            with self.subTest(missing=side):self.assertFalse(self.same())

    def test_coupled_management_time_still_requires_one_positive_generation_advance(self):
        for value in (0,1,3,True,'2',None):
            self.setUp();self.after['application']['metadata']['generation']=value
            with self.subTest(value=value):self.assertFalse(self.same())
        self.setUp()
        for s in [self.before,self.after]:del s['application']['metadata']['generation']
        self.assertFalse(self.same())

    def test_unchanged_version_or_backward_future_observation_is_still_refused(self):
        for mode in ('same_version','backward','future','same_time'):
            self.setUp();app=self.after['application']
            if mode=='same_version':app['metadata']['resourceVersion']=self.before['application']['metadata']['resourceVersion']
            else:
                moment={'backward':self.now-timedelta(seconds=30),'future':self.now+timedelta(seconds=1),
                        'same_time':self.now-timedelta(seconds=20)}[mode]
                app['status']['reconciledAt']=moment.isoformat();app['metadata']['managedFields'][1]['time']=moment.isoformat()
            with self.subTest(mode=mode):self.assertFalse(self.same())

    def test_normal_bookkeeping_cannot_hide_any_other_target_or_lock_change(self):
        paths=[('application','metadata','uid'),('application','metadata','annotations'),
               ('application','spec','source'),('application','status','health'),
               ('application','operation'),('deployment','metadata','resourceVersion'),
               ('deployment','image'),('lease','holder'),('lease','metadata','resourceVersion')]
        for path in paths:
            self.setUp();target=self.after
            for key in path[:-1]:target=target[key]
            target[path[-1]]='changed'
            with self.subTest(path=path):self.assertFalse(self.same())

    def test_missing_malformed_management_or_wrong_resource_type_is_refused(self):
        for field,value in [('managedFields',None),('managedFields',{}),('managedFields',[]),('managedFields',['bad'])]:
            self.setUp();self.after['application']['metadata'][field]=value
            with self.subTest(value=value):self.assertFalse(self.same())
        for field,value in [('apiVersion','other/v1'),('kind','Other')]:
            self.setUp()
            for s in [self.before,self.after]:s['application'][field]=value
            with self.subTest(field=field):self.assertFalse(self.same())


class BoundControllerObservationTest(unittest.TestCase):
    def gate(self, change=lambda snapshot:None):
        fixture=stability.ObservationStabilityTest();self.addCleanup(fixture.doCleanups)
        def prepare(snapshot):
            app=snapshot['application'];app.update(apiVersion='argoproj.io/v1alpha1',kind='Application')
            app['metadata']['managedFields']=[controller_entry(app['status']['reconciledAt'])]
        def update(snapshot):
            app=snapshot['application'];app['metadata']['generation']=2
            app['metadata']['managedFields'][0]['time']=app['status']['reconciledAt']
            change(snapshot)
        return fixture.bound_preflight(mutate=update,prepare=prepare)

    def test_real_c03_gate_accepts_controller_observation_but_never_grants_execution(self):
        result=self.gate()
        self.assertTrue(result['preflight_ready']);self.assertFalse(result['execution_allowed'])

    def test_coupled_observation_in_bound_gate_still_refuses_target_and_lock_changes(self):
        def uid(s):s['application']['metadata']['uid']='different'
        def source(s):s['application']['spec']['source']['targetRevision']='f'*40
        def lease(s):s['lease']['metadata']['resourceVersion']='different'
        for change in (uid,source,lease):
            with self.subTest(change=change.__name__),self.assertRaises((ApprovalRejected,ValueError)):
                self.gate(change)


if __name__=='__main__':unittest.main()
