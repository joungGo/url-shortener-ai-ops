"""합성 Slack 서명→로컬 HTTP→기존 큐/worker→가짜 Slack 스레드 회신을 검증한다."""

import json
from pathlib import Path
import socket
import sys
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))

from ai_ops.slack_adapter import SlackAdapter
from test_slack_adapter import SECRET, SlackHTTPTest


def main():
    f=SlackHTTPTest();f.setUp()
    try:
        server=f.server();original=socket.create_connection;local_calls=[]
        def loopback_only(address,*args,**kwargs):
            if tuple(address)!=server.server_address:raise AssertionError('external_network_forbidden')
            local_calls.append(tuple(address))
            return original(address,*args,**kwargs)
        with patch('socket.create_connection',side_effect=loopback_only), \
             patch.object(f.queue.engine,'load_api_key',side_effect=AssertionError('model_key_forbidden')):
            started=time.monotonic();status,receipt=f.post(server,*f.signed());elapsed=time.monotonic()-started
            assert status==200 and receipt['accepted'] and not receipt['queue_accepted'] and elapsed<3
            queued=f.adapter.drain_one();f.fixture.worker().run_once()
            assert f.tasks.result(queued['task_id'])['investigation_state']=='partial'
            reply=f.adapter.reply(receipt['request_id'],f.transport)
            assert reply['state']=='delivered' and not reply['human_read_confirmed'] and not reply['execution_allowed']
            assert f.transport.last_payload['thread_ts']==f.event['event']['thread_ts']
            assert f.transport.last_payload['channel']==f.event['event']['channel']
            assert 'private-secret-value' not in json.dumps(f.transport.last_payload)
            reopened=SlackAdapter(f.tasks,f.policy,SECRET)
            assert reopened.receive(*f.signed())['duplicate']
            assert reopened.drain_one()['state']=='idle'
            assert reopened.reply(receipt['request_id'],f.transport)['attempts']==1 and f.transport.calls==1
        summary={'status':'passed','inbound':'signed_fixture_http_on_loopback','outbound':'local_slack_fixture_only',
            'ack_elapsed_seconds':round(elapsed,6),'ack_before_queue':True,'authorized_same_channel_thread':True,
            'existing_queue_and_worker':True,'verified_report':'rules_partial','restart_duplicate_reused':True,
            'fixture_reply_posts':f.transport.calls,'real_slack_posts':0,'loopback_http_calls':len(local_calls),
            'external_network_calls':0,'model_calls':0,'fault_injections':0,'recovery_executions':0,
            'human_read_confirmed':False,'execution_allowed':False}
    finally:
        assert f.doCleanups(), 'local_fixture_cleanup_failed'
    summary['server_closed']=server.socket.fileno()==-1
    assert summary['server_closed']
    print(json.dumps(summary))


if __name__=='__main__':main()
