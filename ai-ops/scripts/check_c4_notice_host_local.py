"""느린 호스트 조회의 30초 대기를 재현한다. native 도구/실제 승인을 사용하지 않는다."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_c_trial_notice_host import HostProcessTest


def serial_monitor_wait(seconds):
    fixture=HostProcessTest();fixture.setUp()
    bridge=runner=None
    try:
        args=fixture.args
        env={**os.environ,'AI_OPS_FILES_ROOT':str(fixture.root)}
        bridge=subprocess.Popen([sys.executable,str(fixture.scripts/'bridge_c4_review_notice.py'),
            '--when-ready','--host-events','--plan',str(args.intent),'--activation',str(args.activation),
            '--socket',args.review_notifier_socket],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,env=env)
        assert select.select([bridge.stdout],[],[],5)[0]
        assert json.loads(bridge.stdout.readline())=={'status':'review_bridge_bound'}
        runner=subprocess.Popen([sys.executable,str(fixture.scripts/'run_c4_ready_trial.py'),str(fixture.root)],
                                stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
        assert select.select([bridge.stdout],[],[],5)[0]
        # 이전 별도 채널 운영에서 재현 가능한 순서: 느린 조회 완료 뒤 안내 채널 확인.
        started=time.monotonic()
        subprocess.run([sys.executable,'-c',f'import time;time.sleep({seconds!r})'],check=True)
        blocked=time.monotonic()-started
        event=json.loads(bridge.stdout.readline())
        assert event['status']=='review_notice_requested'
        bridge.stdin.write(b'{"delivery_id":"synthetic-serial-local-only"}\n');bridge.stdin.flush()
        assert runner.wait(timeout=5)==0
        assert bridge.wait(timeout=5)==0
        receipt=json.loads((fixture.root/'live/review-notified.json').read_text())
        latency=(datetime.fromisoformat(receipt['notified_at'])-datetime.fromisoformat(event['emitted_at'])).total_seconds()
        assert latency>=seconds
        return blocked,latency
    finally:
        for p in (runner,bridge):
            if p is not None:
                if p.poll() is None:p.terminate();p.wait(timeout=5)
                for stream in (p.stdin,p.stdout,p.stderr):
                    if stream is not None:stream.close()
        fixture.doCleanups()


def priority_wait():
    fixture=HostProcessTest();fixture.setUp()
    try:
        code,events=fixture.run_host()
        assert code==0
        request=next(e for e in events if e.get('status')=='review_notice_requested')
        receipt=json.loads((fixture.root/'live/review-notified.json').read_text())
        latency=(datetime.fromisoformat(receipt['notified_at'])-datetime.fromisoformat(request['emitted_at'])).total_seconds()
        return latency
    finally:fixture.doCleanups()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--busy-seconds',type=float,default=30)
    args=parser.parse_args()
    if not 1<=args.busy_seconds<=30:parser.error('합성 대기는 1~30초다')
    blocked,old=serial_monitor_wait(args.busy_seconds)
    new=priority_wait()
    assert new<args.busy_seconds
    print(json.dumps({'status':'passed','fixture_only':True,'blocking_monitor_seconds':blocked,
        'serial_emission_to_receipt_seconds':old,'priority_emission_to_receipt_seconds':new,
        'notice_seen_before_owner_completion':True,'native_tool_delivery_verified':False,
        'historical_delay_substage_cause_confirmed':False,'external_calls':0,'fault_injections':0,
        'human_reviews_created':0,'terraform_executed':False}))


if __name__=='__main__':main()
