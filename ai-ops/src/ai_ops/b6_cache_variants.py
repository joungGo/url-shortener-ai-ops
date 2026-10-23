"""실습 전용 캐시 우회·JSON 오류·미설정을 관측하고 각 변경을 원복한다."""

from datetime import datetime, timezone
import hashlib
import json
import math
import subprocess
import time


CACHE_PROGRAM = r'''
import json,os,re,sys,time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import redis
from prometheus_client.parser import text_string_to_metric_families
assert os.environ['APP_ENV']=='practice' and os.environ['ENABLE_FAULT_INJECTION']=='true'
assert urlsplit(os.environ['DATABASE_URL']).hostname=='lab-postgres.ai-ops-lab.svc'
assert os.environ['DATABASE_URL']==os.environ['DATABASE_READ_URL']
action,run_id,variant=sys.argv[1:]
assert re.fullmatch(r'[a-f0-9]{32}',run_id) and variant in ('bypass','json','unconfigured')
assert action in ('prepare','observe','cleanup')
normal='redis://lab-redis.ai-ops-lab.svc:6379/0'
assert os.environ['REDIS_URL'] in (normal,'')
if action!='cleanup': assert (os.environ['REDIS_URL']=='')==(variant=='unconfigured')
cache=redis.from_url(normal,decode_responses=True,socket_timeout=3,socket_connect_timeout=3)
assert cache.ping()
name='b6-cache-'+run_id+'-'+variant
path=Path('/tmp/'+name+'.json'); assert not path.is_symlink()
calls=0
state=json.loads(path.read_text()) if path.exists() else {'name':name}
assert state['name']==name
if 'id' in state: assert type(state['id']) is int and state['id']>0
if 'key' in state:
    assert 'id' in state and re.fullmatch('item:'+str(state['id'])+r':v[0-9]+',state['key'])
def save():
    path.write_text(json.dumps(state)); path.chmod(0o600)
def request(method,route,data=None,headers=None):
    global calls
    calls+=1; assert calls<=10
    req=Request('http://127.0.0.1:8000'+route,method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={'Content-Type':'application/json',**(headers or {})})
    try:
        with urlopen(req,timeout=10) as response:
            raw=response.read(262145); assert len(raw)<=262144
            return response.status,json.loads(raw) if raw else None
    except HTTPError as error: return error.code,None
def metrics():
    global calls
    calls+=1; assert calls<=10
    with urlopen('http://127.0.0.1:8000/metrics/',timeout=8) as response:
        raw=response.read(262145); assert len(raw)<=262144
    values={}; count=None
    for family in text_string_to_metric_families(raw.decode()):
        for sample in family.samples:
            if sample.name=='cache_operation_total' and sample.labels.get('endpoint')=='get_item':
                values[sample.labels['result']]=sample.value
            if sample.name=='db_query_latency_seconds_count' and sample.labels.get('operation')=='select_one':
                count=sample.value
    return {'cache':values,'db_count':count}
def own_read(headers=None):
    status,body=request('GET','/items/'+str(state['id']),headers=headers)
    assert status==200 and body['name']==name and body['id']==state['id']
    return status
restored=False
if action=='prepare':
    assert not path.exists(); save()
    status,roles=request('GET','/items/_db')
    assert status==200 and roles['write']['in_recovery'] is False and roles['read']['in_recovery'] is False
    status,body=request('POST','/items',{'name':name,'description':'B6 disposable cache variant'})
    assert status==201 and body['name']==name and type(body['id']) is int
    state['id']=body['id']; save()
    if variant!='unconfigured':
        own_read(); own_read()
        generation=cache.get('item:'+str(state['id'])+':version') or '0'
        state['key']='item:'+str(state['id'])+':v'+generation
        assert cache.pttl('item:'+str(state['id'])+':primary-read')==-2
        original=cache.get(state['key']); ttl=cache.pttl(state['key'])
        assert original is not None and 0<ttl<=300000
        value=json.loads(original); assert value['id']==state['id'] and value['name']==name
        state.update(original=original,ttl_at_prepare_ms=ttl); save()
    result={'created':True,'redis_configured':bool(os.environ['REDIS_URL']),
        'database_roles':{'write_in_recovery':False,'read_in_recovery':False},
        'baseline':metrics(),'redis_ping':True,'http_calls':calls}
elif action=='observe':
    assert 'id' in state and path.exists()
    before=metrics(); key_unchanged=None
    if variant=='json':
        # 자기 아이템의 현재 세대 키만 변경하고 원래 TTL을 유지한다.
        with cache.pipeline() as pipe:
            pipe.watch(state['key']); current=pipe.get(state['key'])
            assert current in (state['original'],'{') and pipe.pttl(state['key'])>0
            pipe.multi(); pipe.set(state['key'],'{',xx=True,keepttl=True)
            assert pipe.execute()==[True]
    status=own_read({'X-Week12-Bypass-Cache':'true'} if variant=='bypass' else None)
    after=metrics()
    if variant=='bypass': key_unchanged=cache.get(state['key'])==state['original']; assert key_unchanged
    if variant=='json': assert cache.get(state['key'])=='{'
    result={'http_status':status,'counters_before':before['cache'],'counters_after':after['cache'],
        'db_count_before':before['db_count'],'db_count_after':after['db_count'],
        'redis_configured':bool(os.environ['REDIS_URL']),'redis_ping':bool(cache.ping()),
        'json_invalid_injected':variant=='json','bypass_header_sent':variant=='bypass',
        'delay_header_sent':False,'cached_value_unchanged':key_unchanged,'http_calls':calls}
else:
    # Pod 교체로 소유 파일을 잃었어도 고유 이름에 해당하는 자기 행만 찾는다.
    if 'id' not in state:
        from app.core.database import WriteSession
        from app.models.item import Item
        with WriteSession() as db:
            rows=db.query(Item).filter(Item.name==name).limit(2).all(); assert len(rows)<=1
            if rows: state['id']=rows[0].id
    if variant=='json' and 'key' in state:
        with cache.pipeline() as pipe:
            pipe.watch(state['key']); current=pipe.get(state['key']); ttl=pipe.pttl(state['key'])
            assert current in (state['original'],'{',None)
            if current=='{':
                assert ttl>0
                pipe.multi(); pipe.psetex(state['key'],ttl,state['original']); assert pipe.execute()==[True]
            restored=True
    if 'id' in state:
        status,body=request('GET','/items/'+str(state['id']))
        assert status==404 or (status==200 and body['name']==name)
        if status==200:
            status,_=request('DELETE','/items/'+str(state['id'])); assert status==204
        status,_=request('GET','/items/'+str(state['id'])); assert status==404
        keys=['items:primary-read','item:'+str(state['id'])+':primary-read']
        deadline=time.monotonic()+10
        while any(cache.exists(key) for key in keys):
            assert time.monotonic()<deadline
            time.sleep(0.1)
    status,_=request('GET','/readyz'); assert status==200 and cache.ping()
    if path.exists(): path.unlink()
    result={'owned_data_removed':True,'json_value_restored':restored if variant=='json' else None,
        'markers_expired':True,'redis_ping':True,'http_status':status,'http_calls':calls}
print(json.dumps(result))
'''


def check_cache_observation(value, variant):
    """JSON 실패의 hit/error 동시 증가와 미설정 unavailable을 구분한다."""
    expected = {"bypass": {"bypass"}, "json": {"hit", "error"}, "unconfigured": {"unavailable"}}[variant]
    if (value.get("http_status") != 200 or value.get("redis_ping") is not True
            or value.get("redis_configured") is not (variant != "unconfigured")
            or value.get("json_invalid_injected") is not (variant == "json")
            or value.get("bypass_header_sent") is not (variant == "bypass")
            or value.get("delay_header_sent") is not False):
        raise ValueError("캐시 하위 사례 설정·응답이 다릅니다")
    before, after = value["counters_before"], value["counters_after"]
    for key in set(before) | set(after) | expected:
        if key in expected:
            if after.get(key) != (before[key]+1 if key in before else 1):
                raise ValueError("캐시 결과 증가가 하위 사례와 다릅니다")
        elif before.get(key) != after.get(key):
            raise ValueError("다른 캐시 결과가 섞였습니다")
    old, new = value["db_count_before"], value["db_count_after"]
    if (any(x is not None and (type(x) not in (int,float) or not math.isfinite(x) or x<0) for x in (old,new))
            or new != (old+1 if old is not None else 1)):
        raise ValueError("DB fallback 조회 증가를 확인하지 못했습니다")
    if variant == "bypass" and value.get("cached_value_unchanged") is not True:
        raise ValueError("우회 요청이 기존 캐시를 바꿨습니다")


def cache_report_check(report, variant):
    """관측 지표·설정 근거와 보고서의 단정 범위를 비교한다."""
    expected = {"bypass":"bypass", "json":"error", "unconfigured":"unavailable"}[variant]
    visible = any(s["outcome"]==expected and s["value"]>0
                  for s in report.get("metric_samples",{}).get("cache_outcomes",[]))
    configured = report.get("cache_configured_values",[])
    if not (visible and configured and all(v is (variant!="unconfigured") for v in configured)
            and report.get("current_state")=="unknown"):
        return "needs_review"
    if variant=="json":
        supported = any("ev-metric-cache_outcomes" in ids for ids in report.get("hypothesis_evidence_ids",[]))
        return "generic_cache_error_only" if (report.get("diagnosis_status")=="suspected_cause"
            and report.get("hypotheses")==1 and supported and report.get("generic_cache_error_only") is True) else "needs_review"
    cited = any("ev-metric-cache_outcomes" in ids for ids in report.get("fact_evidence_ids",[]))
    return "no_false_cause" if report.get("hypotheses")==0 and cited else "needs_review"


def cache_setting(client):
    """고정 실습 앱의 공개 Redis 설정과 Pod 정의 해시만 반환한다."""
    from ai_ops.b6_lab import NAMESPACE, PURPOSE, ECR
    obj=client.get(NAMESPACE,"deployment","lab-api")
    spec=obj["spec"]; containers=spec["template"]["spec"]["containers"]
    if (any(obj["metadata"].get("labels",{}).get(k)!=v for k,v in PURPOSE.items())
            or spec["replicas"]!=1 or spec["strategy"]["type"]!="Recreate"
            or len(containers)!=1 or containers[0]["name"]!="api"
            or not containers[0]["image"].startswith(ECR+"urlshortener@sha256:")):
        raise ValueError("고정 실습 앱과 다릅니다")
    entries=[e for e in containers[0].get("env",[]) if e.get("name")=="REDIS_URL"]
    if (len(entries)!=1 or not set(entries[0])<={"name","value"}
            or entries[0].get("value","") not in ("","redis://lab-redis.ai-ops-lab.svc:6379/0")):
        raise ValueError("복원할 공개 Redis 설정을 확인할 수 없습니다")
    return {"uid":obj["metadata"]["uid"],"generation":obj["metadata"]["generation"],
        "image":containers[0]["image"],"redis_configured":bool(entries[0].get("value","")),
        "template_sha256":hashlib.sha256(json.dumps(spec["template"],sort_keys=True,separators=(",",":")).encode()).hexdigest()}


def toggle_cache(client, baseline, *, disabled):
    """UID·현재 값·이미지가 맞을 때 REDIS_URL 한 필드만 바꾼다."""
    from ai_ops.b6_lab import NAMESPACE
    current=cache_setting(client)
    if current["uid"]!=baseline["uid"] or current["image"]!=baseline["image"]:
        raise ValueError("기준선과 다른 실습 앱은 변경하지 않습니다")
    value="" if disabled else "redis://lab-redis.ai-ops-lab.svc:6379/0"
    if current["redis_configured"] is (not disabled):
        if not disabled:
            client.command("-n",NAMESPACE,"rollout","status","deployment/lab-api","--timeout=180s",timeout=195)
        return
    if disabled and current["template_sha256"]!=baseline["template_sha256"]:
        raise ValueError("다른 실습 설정이 변경됐습니다")
    obj=client.get(NAMESPACE,"deployment","lab-api")
    from ai_ops.b6_coordination import require_manual_deployment
    require_manual_deployment(obj)
    containers=obj["spec"]["template"]["spec"]["containers"]
    if obj["metadata"]["uid"]!=baseline["uid"] or containers[0]["image"]!=baseline["image"]:
        raise ValueError("설정 변경 중 실습 앱이 교체됐습니다")
    entries=containers[0]["env"]; positions=[i for i,e in enumerate(entries) if e.get("name")=="REDIS_URL"]
    if len(positions)!=1:
        raise ValueError("Redis 설정 위치가 다릅니다")
    index=positions[0]; old="redis://lab-redis.ai-ops-lab.svc:6379/0" if disabled else ""
    if entries[index]!={"name":"REDIS_URL","value":old} and not (not disabled and entries[index]=={"name":"REDIS_URL"}):
        raise ValueError("다른 Redis 설정을 덮어쓰지 않습니다")
    patch=[{"op":"test","path":"/metadata/uid","value":baseline["uid"]},
        {"op":"test","path":"/metadata/resourceVersion","value":obj["metadata"]["resourceVersion"]},
        {"op":"test","path":"/spec/template/spec/containers/0/image","value":baseline["image"]},
        {"op":"test","path":f"/spec/template/spec/containers/0/env/{index}","value":entries[index]},
        {"op":"replace" if "value" in entries[index] else "add",
         "path":f"/spec/template/spec/containers/0/env/{index}/value","value":value}]
    client.command("-n",NAMESPACE,"patch","deployment","lab-api","--type=json","--patch",json.dumps(patch))
    client.command("-n",NAMESPACE,"rollout","status","deployment/lab-api","--timeout=180s",timeout=195)


def execute_cache_variants(client, run_id, *, sleep=time.sleep, event=None, checkpoint=None, recovery_plan=None,
                           variants=("bypass","json","unconfigured")):
    """세 변형을 직렬 수행하고 미설정 뒤 설정·이미지·자료를 원복한다."""
    def now(): return datetime.now(timezone.utc).isoformat()
    notify = event or (lambda _:None)
    if variants not in (("bypass","json","unconfigured"),("unconfigured",)):
        raise ValueError("전체 사례 또는 미설정 한 사례만 실행할 수 있습니다")
    result = {"scenario":"S11", "run_id":run_id,"namespace":"ai-ops-lab","status":"failed",
        "started_at":now(),"cases":[],"http_calls":0,"http_call_limit":48,"external_model_calls":0,
        "scope":"bypass_json_unconfigured_variants_single_primary_not_live_model","selected_variants":list(variants)}
    try:
        before,identity = client.production(),client.guard()
        setting = client.cache_setting()
        if not setting["redis_configured"] or recovery_plan is None or checkpoint is None:
            raise ValueError("복구 설정·사전 관측 기록이 필요합니다")
        result["baseline"] = identity
    except (ValueError,OSError,KeyError,subprocess.TimeoutExpired):
        result.update(status="blocked",failure_reason="preflight_failed",cleanup={"no_mutation_started":True},finished_at=now())
        return result
    cleanup = {}; config_attempted=False
    try:
        for variant in variants:
            case={"variant":variant,"observations":[],"started_at":now()}; attempted=False
            result["cases"].append(case)
            try:
                if variant=="unconfigured":
                    recovery_plan({"record_type":"pre_mutation_cache_setting_recovery_plan","scenario":"S11",
                        "run_id":run_id,"baseline":setting,"disabled_value":"","created_at":now()})
                    config_attempted=True
                    client.toggle_cache(setting,disabled=True)
                    notify("실습 앱의 REDIS_URL만 비활성화·새 Pod 준비 완료. 이전 5분 지표 창과 새 scrape 표본 대기")
                    # 이전 Pod의 JSON error를 미설정 원인으로 섞지 않도록 전체 창을 비운다.
                    sleep(305)
                    client.guard()
                attempted=True
                cleanup[variant+"_data_removed"]=False
                if variant=="json": cleanup["json_restored"]=False
                prepared=client.cache_variant("prepare",run_id,variant)
                result["http_calls"]+=prepared["http_calls"]; case["preparation"]=prepared
                for _ in range(2):
                    observation=client.cache_variant("observe",run_id,variant)
                    result["http_calls"]+=observation["http_calls"]
                    check_cache_observation(observation,variant)
                    case["observations"].append({"at":now(),**observation})
                    notify("캐시 "+variant+" 실제 응답·결과 counter·DB fallback 확인, scrape 대기")
                    sleep(35)
                case["ground_truth_recorded_at"]=now()
                checkpoint({**case,"scenario":"S11","run_id":run_id,"status":"observations_recorded","execution_status_final":False})
                case["report"]=client.collect(run_id+"-"+variant)
                case["report_check"]=cache_report_check(case["report"],variant)
            finally:
                if attempted:
                    recovered=client.cache_variant("cleanup",run_id,variant)
                    result["http_calls"]+=recovered["http_calls"]; case["recovery"]=recovered
                    cleanup[variant+"_data_removed"]=recovered["owned_data_removed"] and recovered["markers_expired"]
                    if variant=="json": cleanup["json_restored"]=recovered["json_value_restored"] is True
                case["finished_at"]=now()
        result["status"]="needs_review" if any(c["report_check"]=="needs_review" for c in result["cases"]) else "passed"
    except (ValueError,OSError,KeyError,TypeError,subprocess.TimeoutExpired):
        result["failure_reason"]="cache_variant_failed"
    finally:
        if config_attempted:
            try:
                client.toggle_cache(setting,disabled=False)
                cleanup["cache_setting_restored"]=client.cache_setting()["template_sha256"]==setting["template_sha256"]
                health=client.app("health",run_id)
                result["http_calls"]+=health["http_calls"]; result["restored_health"]=health
                cleanup["restored_http_and_redis"]=health["redis_ping"] and all(v==200 for v in health["http"].values())
            except (ValueError,OSError,KeyError,subprocess.TimeoutExpired):
                cleanup["cache_setting_restored"]=False
        try:
            cleanup["production_unchanged"]=client.production()==before
            current=client.guard(require_quality=False)
            cleanup["lab_uids_images_unchanged"]=all(current[k]["uid"]==v["uid"] and current[k]["image"]==v["image"]
                and (k=="lab-api" or current[k]["generation"]==v["generation"]) for k,v in identity.items())
        except (ValueError,OSError,KeyError,subprocess.TimeoutExpired): cleanup["identity_check_failed"]=False
        if not all(cleanup.values()) or len(result["cases"])!=len(variants) or any("recovery" not in c for c in result["cases"]):
            result["status"]="recovery_failed" if not all(cleanup.values()) else "failed"
        if result["http_calls"]>result["http_call_limit"]:
            result.update(status="failed",failure_reason="request_budget_exceeded")
        result.update(cleanup=cleanup,finished_at=now())
    return result
