"""실습 목록의 최근 쓰기 표식·자연 만료·캐시 경로를 제한된 요청으로 검증한다."""

from datetime import datetime, timezone
import math
import subprocess
import time


CONSISTENCY_PROGRAM = r'''
import json,os,re,sys,time
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import redis
from prometheus_client.parser import text_string_to_metric_families
assert os.environ['APP_ENV']=='practice' and os.environ['ENABLE_FAULT_INJECTION']=='true'
assert urlsplit(os.environ['DATABASE_URL']).hostname=='lab-postgres.ai-ops-lab.svc'
assert os.environ['DATABASE_URL']==os.environ['DATABASE_READ_URL']
assert urlsplit(os.environ['REDIS_URL']).hostname=='lab-redis.ai-ops-lab.svc'
assert os.environ['REPLICA_CONSISTENCY_WINDOW_SECONDS']=='5'
action,run_id=sys.argv[1:]; assert re.fullmatch(r'[a-f0-9]{32}',run_id)
assert action in ('round-0','round-1','cleanup')
path=Path('/tmp/b6-consistency-'+run_id+'.json'); assert not path.is_symlink()
cache=redis.from_url(os.environ['REDIS_URL'],decode_responses=True,socket_timeout=3,socket_connect_timeout=3)
assert cache.ping()
calls=0
def now(): return datetime.now(timezone.utc).isoformat()
def save(records):
    path.write_text(json.dumps(records)); path.chmod(0o600)
def request(method,route,data=None):
    global calls
    calls+=1
    req=Request('http://127.0.0.1:8000'+route,method=method,
        data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'})
    try:
        with urlopen(req,timeout=10) as response:
            raw=response.read(262145); assert len(raw)<=262144
            return response.status,json.loads(raw) if raw else None
    except HTTPError as error: return error.code,None
def metrics():
    global calls
    calls+=1
    with urlopen('http://127.0.0.1:8000/metrics/',timeout=8) as response:
        raw=response.read(262145); assert len(raw)<=262144
    counters={}; db_count=None
    for family in text_string_to_metric_families(raw.decode()):
        for sample in family.samples:
            if sample.name=='cache_operation_total' and sample.labels.get('endpoint')=='list_items':
                counters[sample.labels['result']]=sample.value
            if sample.name=='db_query_latency_seconds_count' and sample.labels.get('operation')=='select_all':
                db_count=sample.value
    return {'cache':counters,'db_count':db_count}
def wait_absent(key,limit):
    deadline=time.monotonic()+limit
    while cache.exists(key):
        assert time.monotonic()<deadline,'natural expiry timeout'
        time.sleep(0.1)
    assert cache.pttl(key)==-2
records=json.loads(path.read_text()) if path.exists() else []
if action=='cleanup':
    marker_keys=['items:primary-read']
    for index,value in enumerate(records):
        assert value['name']=='b6-consistency-'+run_id+'-'+str(index)
        # 생성 응답이 유실됐으면 고유 이름의 행만 찾고 다른 자료는 삭제하지 않는다.
        if 'id' not in value:
            from app.core.database import WriteSession
            from app.models.item import Item
            with WriteSession() as db:
                rows=db.query(Item).filter(Item.name==value['name']).limit(2).all(); assert len(rows)<=1
                if rows: value['id']=rows[0].id
        if 'id' in value:
            status,body=request('GET','/items/'+str(value['id']))
            assert status==404 or (status==200 and body['name']==value['name'])
            if status==200:
                status,_=request('DELETE','/items/'+str(value['id'])); assert status==204
            status,_=request('GET','/items/'+str(value['id'])); assert status==404
            marker_keys.append('item:'+str(value['id'])+':primary-read')
    for key in marker_keys: wait_absent(key,10)
    status,_=request('GET','/readyz'); assert status==200 and cache.ping()
    if path.exists(): path.unlink()
    result={'owned_data_removed':True,'markers_expired':True,'redis_ping':True,'http_status':status}
else:
    index=int(action[-1]); assert len(records)==index
    if index==0: assert not path.exists()
    roles=None
    if index==0:
        status,body=request('GET','/items/_db')
        assert status==200 and body['write']['in_recovery'] is False and body['read']['in_recovery'] is False
        roles={'write_in_recovery':False,'read_in_recovery':False,'same_configured_database':True}
    baseline=metrics()
    value={'name':'b6-consistency-'+run_id+'-'+str(index)}
    records.append(value); save(records)
    write_started=now()
    status,body=request('POST','/items',{'name':value['name'],'description':'B6 disposable consistency test'})
    assert status==201 and body['name']==value['name'] and type(body['id']) is int
    value['id']=body['id']; save(records)
    write_completed=now()
    generation=cache.get('items:version') or '0'; key='items:all:v'+generation
    def read(phase,before):
        assert (cache.get('items:version') or '0')==generation,'concurrent list generation change'
        marker_before=cache.pttl('items:primary-read'); data_before=cache.pttl(key)
        at=now(); status,body=request('GET','/items')
        assert status==200 and isinstance(body,list)
        assert len([item for item in body if item['id']==value['id'] and item['name']==value['name']])==1
        marker_after=cache.pttl('items:primary-read'); data_after=cache.pttl(key)
        assert (cache.get('items:version') or '0')==generation,'concurrent list generation change'
        after=metrics()
        return {'phase':phase,'at':at,'http_status':status,'marker_before_ms':marker_before,'marker_after_ms':marker_after,
            'cache_before_ms':data_before,'cache_after_ms':data_after,'counters_before':before['cache'],
            'counters_after':after['cache'],'db_count_before':before['db_count'],'db_count_after':after['db_count']},after
    observations=[]
    item,previous=read('recent_write',baseline); observations.append(item)
    wait_absent('items:primary-read',10)
    item,previous=read('marker_expired_cache_hit',previous); observations.append(item)
    # Primary 조회도 캐시를 채우므로 표식 만료 직후의 hit와 캐시 자체의 자연 만료를 구분한다.
    wait_absent(key,70)
    item,previous=read('cache_expired_read_session',previous); observations.append(item)
    item,previous=read('warm_cache_hit',previous); observations.append(item)
    result={'round':index,'write_started_at':write_started,'write_completed_at':write_completed,
        'observations':observations,'database_roles':roles,'marker_ttl_seconds':5,'natural_list_cache_expiry':True,
        'bypass_header_sent':False,'delay_header_sent':False,'redis_ping':True}
result['http_calls']=calls
print(json.dumps(result))
'''


def check_consistency_round(value):
    """표식·캐시 TTL·결과 counter·DB 표본을 함께 확인하고 논리 경로만 판정한다."""
    if (value.get("marker_ttl_seconds") != 5 or value.get("natural_list_cache_expiry") is not True
            or value.get("bypass_header_sent") is not False or value.get("delay_header_sent") is not False
            or value.get("redis_ping") is not True):
        raise ValueError("실습 표식·자연 만료·헤더 조건이 다릅니다")
    observations = value.get("observations", [])
    phases = ("recent_write", "marker_expired_cache_hit", "cache_expired_read_session", "warm_cache_hit")
    if [item.get("phase") for item in observations] != list(phases):
        raise ValueError("최근 쓰기와 자연 만료의 순서가 다릅니다")
    roles = value.get("database_roles")
    if value.get("round") == 0 and roles != {"write_in_recovery": False, "read_in_recovery": False,
                                            "same_configured_database": True}:
        raise ValueError("실제 단일 Primary 역할을 확인하지 못했습니다")
    for index, item in enumerate(observations):
        outcome = ("consistency_primary", "hit", "miss", "hit")[index]
        before, after = item["counters_before"], item["counters_after"]
        if item["http_status"] != 200 or set(after) - set(before) - {outcome}:
            raise ValueError("예상하지 않은 목록 응답·캐시 결과입니다")
        if after.get(outcome) != (before[outcome] + 1 if outcome in before else 1):
            raise ValueError("실제 목록 캐시 결과의 증가가 다릅니다")
        if any(before.get(key) != after.get(key) for key in set(before) | set(after) if key != outcome):
            raise ValueError("실험에 다른 캐시 결과가 섞였습니다")
        db_before, db_after = item["db_count_before"], item["db_count_after"]
        if any(v is not None and (type(v) not in (int, float) or not math.isfinite(v) or v < 0)
               for v in (db_before, db_after)):
            raise ValueError("DB 표본이 유효하지 않습니다")
        if index in (0, 2):
            if db_after != (db_before + 1 if db_before is not None else 1):
                raise ValueError("목록 DB 조회 증가를 확인하지 못했습니다")
        elif db_before != db_after:
            raise ValueError("cache hit 중 DB 조회가 증가했습니다")
        if index == 0:
            if not (0 < item["marker_before_ms"] <= 5000 and 0 < item["marker_after_ms"] <= 5000
                    and 0 < item["cache_after_ms"] <= 60000):
                raise ValueError("Primary 조회가 표식 유효 구간에 있지 않습니다")
        elif item["marker_before_ms"] != -2 or item["marker_after_ms"] != -2:
            raise ValueError("표식이 자연 만료되지 않았습니다")
        if index in (1, 3) and not (0 < item["cache_before_ms"] <= 60000 and 0 < item["cache_after_ms"] <= 60000):
            raise ValueError("실제 cache hit의 TTL이 유효하지 않습니다")
        if index == 2 and not (item["cache_before_ms"] == -2 and 0 < item["cache_after_ms"] <= 60000):
            raise ValueError("목록 캐시 자체의 자연 만료를 확인하지 못했습니다")


def execute_consistency(client, run_id, *, sleep=time.sleep, event=None, checkpoint=None):
    """표식·캐시 만료 관측과 자료 수집을 분리하고 자기 아이템만 finally에서 제거한다."""
    now = lambda: datetime.now(timezone.utc).isoformat()
    result = {"scenario": "S9", "run_id": run_id, "namespace": "ai-ops-lab", "status": "failed",
              "started_at": now(), "rounds": [], "http_calls": 0, "http_call_limit": 28,
              "scope": "list_consistency_single_primary_not_actual_replica", "external_model_calls": 0}
    notify = event or (lambda _: None)
    try:
        before, identity = client.production(), client.guard()
    except (ValueError, OSError, KeyError, subprocess.TimeoutExpired):
        result.update(status="blocked", failure_reason="preflight_failed", cleanup={"no_mutation_started": True}, finished_at=now())
        return result
    attempted = False
    try:
        result["baseline"] = identity
        for index in range(2):
            attempted = True
            notify("목록 쓰기 직후→표식 자연 만료→캐시 자연 만료 관측 중 (최대 약 70초)")
            value = client.consistency("round-" + str(index), run_id)
            result["http_calls"] += value["http_calls"]
            check_consistency_round(value)
            result["rounds"].append(value)
        if result["http_calls"] > 21:
            raise ValueError("관측 요청 한도를 넘었습니다")
        result["ground_truth_recorded_at"] = now()
        if checkpoint:
            checkpoint({**result, "status": "observations_recorded", "execution_status_final": False})
        notify("관측 기록 완료·마지막 Prometheus 표본 대기")
        sleep(35)
        result["report"] = client.collect_consistency(run_id)
        report = result["report"]
        consistency = any(s["outcome"] == "consistency_primary" and s["value"] > 0
                          for s in report.get("metric_samples", {}).get("cache_outcomes", []))
        cited = any("ev-metric-cache_outcomes" in ids for ids in report.get("fact_evidence_ids", []))
        passed = (consistency and cited and report["hypotheses"] == 0 and report["current_state"] == "unknown")
        result["report_check"] = "consistency_observed_no_false_cause" if passed else "needs_review"
        result["status"] = "passed" if passed else "needs_review"
    except (ValueError, OSError, KeyError, TypeError, subprocess.TimeoutExpired):
        result["failure_reason"] = "consistency_observation_failed"
    finally:
        cleanup = {"owned_data_removed": not attempted, "markers_expired": not attempted}
        if attempted:
            try:
                recovered = client.consistency("cleanup", run_id)
                result["http_calls"] += recovered["http_calls"]
                result["recovery_observation"] = recovered
                cleanup.update(owned_data_removed=recovered["owned_data_removed"] and recovered["redis_ping"],
                               markers_expired=recovered["markers_expired"])
            except (ValueError, OSError, KeyError, TypeError, subprocess.TimeoutExpired):
                pass
        try:
            cleanup["production_unchanged"] = client.production() == before
            cleanup["lab_identity_unchanged"] = client.guard() == identity
        except (ValueError, OSError, KeyError, subprocess.TimeoutExpired):
            cleanup["identity_check_failed"] = False
        result.update(cleanup=cleanup, finished_at=now())
        if not all(cleanup.values()):
            result["status"] = "recovery_failed"
        if result["http_calls"] > result["http_call_limit"]:
            result.update(status="failed", failure_reason="request_budget_exceeded")
    return result
