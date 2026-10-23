"""캐시 오류 의미의 경계와 설정 변경/응답 유실 뒤 원복을 검증한다."""

import json
import unittest
from unittest.mock import Mock, patch

from ai_ops.b6_cache_variants import cache_report_check, check_cache_observation, toggle_cache
from ai_ops.b6_lab import PURPOSE, ECR
from ai_ops.b6_scenarios import LabClient, execute


def observation(variant):
    keys={"bypass":["bypass"],"json":["hit","error"],"unconfigured":["unavailable"]}[variant]
    return {"http_status":200,"redis_ping":True,"redis_configured":variant!="unconfigured",
        "json_invalid_injected":variant=="json","bypass_header_sent":variant=="bypass",
        "delay_header_sent":False,"cached_value_unchanged":True if variant=="bypass" else None,
        "counters_before":{},"counters_after":{key:1 for key in keys},
        "db_count_before":None,"db_count_after":1,"http_calls":3}


def report(variant):
    return {"current_state":"unknown","cache_configured_values":[variant!="unconfigured"],
        "hypotheses":1 if variant=="json" else 0,"diagnosis_status":"suspected_cause" if variant=="json" else "insufficient_evidence",
        "generic_cache_error_only":True,"fact_evidence_ids":[["ev-metric-cache_outcomes"]],
        "hypothesis_evidence_ids":[["ev-metric-cache_outcomes"]] if variant=="json" else [],
        "metric_samples":{"cache_outcomes":[{"outcome":{"bypass":"bypass","json":"error","unconfigured":"unavailable"}[variant],"value":1.1}]}}


class CacheLab:
    def __init__(self,fail=None):
        self.fail=fail; self.configured=True; self.actions=[]; self.generation=1
    def production(self): return {"production":"same"}
    def guard(self,require_quality=True):
        if self.fail=="guard": raise ValueError("not ready")
        return {"lab-api":{"uid":"api","generation":self.generation,"image":"fixed"}}
    def cache_setting(self):
        return {"uid":"api","generation":self.generation,"image":"fixed","redis_configured":self.configured,
                "template_sha256":"original" if self.configured else "disabled"}
    def toggle_cache(self,baseline,*,disabled):
        self.actions.append("disable" if disabled else "restore")
        self.configured=not disabled; self.generation+=1
        if disabled and self.fail=="patch-response": raise ValueError("response lost")
        if not disabled and self.fail=="restore": raise ValueError("restore failed")
    def cache_variant(self,action,run,variant):
        self.actions.append((variant,action))
        if self.fail==variant+":"+action: raise ValueError("failed response")
        if action=="prepare": return {"http_calls":3 if variant=="unconfigured" else 5}
        if action=="observe": return observation(variant)
        return {"http_calls":4,"owned_data_removed":True,"markers_expired":True,"json_value_restored":variant=="json"}
    def collect(self,run):
        variant=run.rsplit('-',1)[-1]; self.actions.append((variant,"collect"))
        if self.fail==variant+":collect": raise ValueError("collection failed")
        return report(variant)
    def app(self,action,run):
        self.actions.append("health")
        return {"http_calls":2,"http":{"/readyz":200,"/items":200},"redis_ping":True}


class CacheVariantsTest(unittest.TestCase):
    def run_cache(self,client,**kwargs):
        return execute("S11",client,run_id="1"*32,sleep=kwargs.pop("sleep",lambda _:None),
            checkpoint=kwargs.pop("checkpoint",Mock()),recovery_plan=kwargs.pop("recovery_plan",Mock()),**kwargs)

    def test_targeted_unconfigured_retry_does_not_repeat_passed_cases(self):
        client=CacheLab(); result=self.run_cache(client,cache_variant="unconfigured")
        self.assertEqual(result["status"],"passed")
        self.assertEqual(result["selected_variants"],["unconfigured"])
        self.assertFalse(any(isinstance(a,tuple) and a[0] in ("bypass","json") for a in client.actions))
        self.assertTrue(client.configured)

    def test_each_observation_distinguishes_intended_bypass_json_hit_error_and_unconfigured(self):
        for variant in ("bypass","json","unconfigured"):
            with self.subTest(variant=variant):
                check_cache_observation(observation(variant),variant)
                self.assertNotEqual(cache_report_check(report(variant),variant),"needs_review")

    def test_json_error_requires_both_hit_and_error_not_a_pure_redis_failure(self):
        for counters in ({"error":1},{"hit":1},{"hit":1,"error":1,"unavailable":1}):
            with self.subTest(counters=counters),self.assertRaises(ValueError):
                check_cache_observation({**observation("json"),"counters_after":counters},"json")

    def test_wrong_setting_headers_cache_mutation_and_absent_db_sample_rejected(self):
        for variant,changes in (("unconfigured",{"redis_configured":True}),("bypass",{"cached_value_unchanged":False}),
                ("bypass",{"bypass_header_sent":False}),("json",{"db_count_after":None}),
                ("json",{"db_count_after":float('nan')})):
            with self.subTest(variant=variant,changes=changes),self.assertRaises(ValueError):
                check_cache_observation({**observation(variant),**changes},variant)

    def test_report_cannot_promote_unconfigured_to_failure_or_json_to_server_cause(self):
        for variant,changes in (("unconfigured",{"hypotheses":1}),("json",{"generic_cache_error_only":False}),
                ("json",{"hypothesis_evidence_ids":[]}),("bypass",{"cache_configured_values":[]}),
                ("bypass",{"current_state":"healthy"}),("unconfigured",{"cache_configured_values":[True]})):
            with self.subTest(variant=variant,changes=changes):
                self.assertEqual(cache_report_check({**report(variant),**changes},variant),"needs_review")

    def test_all_cases_checkpoint_before_report_and_disabled_window_then_restore(self):
        client=CacheLab(); truths=[]; waits=[]
        def truth(value):
            self.assertNotIn((value['variant'],'collect'),client.actions)
            truths.append(value['variant'])
        result=self.run_cache(client,checkpoint=truth,sleep=lambda seconds:waits.append(seconds))
        self.assertEqual(result['status'],'passed')
        self.assertEqual(result['http_calls'],45)
        self.assertEqual(truths,['bypass','json','unconfigured'])
        self.assertEqual(waits.count(305),1)
        self.assertEqual(waits.count(35),6)
        self.assertTrue(client.configured)
        self.assertTrue(all(result['cleanup'].values()))

    def test_creation_observation_or_collection_failure_always_cleans_own_case(self):
        for failure in ('bypass:prepare','json:observe','json:collect','unconfigured:prepare','unconfigured:collect'):
            with self.subTest(failure=failure):
                client=CacheLab(fail=failure); result=self.run_cache(client)
                self.assertNotEqual(result['status'],'passed')
                variant=failure.split(':')[0]
                self.assertIn((variant,'cleanup'),client.actions)
                self.assertTrue(client.configured)

    def test_json_cleanup_failure_is_recovery_failure_and_blocks_success(self):
        result=self.run_cache(CacheLab(fail='json:cleanup'))
        self.assertEqual(result['status'],'recovery_failed')
        self.assertFalse(result['cleanup']['json_restored'])

    def test_configuration_patch_response_loss_still_restores_original_setting(self):
        client=CacheLab(fail='patch-response'); result=self.run_cache(client)
        self.assertNotEqual(result['status'],'passed')
        self.assertTrue(client.configured)
        self.assertIn('restore',client.actions)

    def test_failed_restore_cannot_be_success(self):
        result=self.run_cache(CacheLab(fail='restore'))
        self.assertEqual(result['status'],'recovery_failed')
        self.assertFalse(result['cleanup']['cache_setting_restored'])

    def test_missing_recovery_plan_or_preflight_never_changes_resources(self):
        for client,kwargs in ((CacheLab(),{'recovery_plan':None}),(CacheLab(fail='guard'),{})):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.run_cache(client,**kwargs)['status'],'blocked')
                self.assertEqual(client.actions,[])

    def test_disk_failure_before_configuration_change_prevents_patch(self):
        client=CacheLab(); result=self.run_cache(client,recovery_plan=Mock(side_effect=ValueError('disk full')))
        self.assertEqual(result['status'],'failed')
        self.assertNotIn('disable',client.actions)

    def test_patch_tests_uid_version_image_and_current_field(self):
        obj={'metadata':{'uid':'api','generation':1,'resourceVersion':'7','labels':PURPOSE},'spec':{'replicas':1,
            'strategy':{'type':'Recreate'},'template':{'spec':{'containers':[{'name':'api','image':ECR+'urlshortener@sha256:'+'a'*64,
                'env':[{'name':'REDIS_URL','value':'redis://lab-redis.ai-ops-lab.svc:6379/0'}]}]}}}}
        client=LabClient()
        with patch.object(client,'get',return_value=obj),patch.object(client,'command') as command:
            baseline=client.cache_setting(); toggle_cache(client,baseline,disabled=True)
            operations=json.loads(command.call_args_list[0].args[-1])
            self.assertEqual([p['op'] for p in operations],['test','test','test','test','replace'])
            self.assertEqual(operations[-1]['value'],'')
            self.assertEqual(operations[1]['value'],'7')
            # API가 빈 value를 생략한 실제 형태에서도 원래 값을 add로 복구한다.
            obj['spec']['template']['spec']['containers'][0]['env']=[{'name':'REDIS_URL'}]
            command.reset_mock()
            self.assertFalse(client.cache_setting()['redis_configured'])
            toggle_cache(client,baseline,disabled=False)
            operations=json.loads(command.call_args_list[0].args[-1])
            self.assertEqual(operations[-1]['op'],'add')
            self.assertEqual(operations[-2]['value'],{'name':'REDIS_URL'})
            self.assertEqual(operations[-1]['value'],'redis://lab-redis.ai-ops-lab.svc:6379/0')
        baseline['uid']='other'
        with patch.object(client,'get',return_value=obj),patch.object(client,'command') as command:
            with self.assertRaises(ValueError): toggle_cache(client,baseline,disabled=True)
            command.assert_not_called()
