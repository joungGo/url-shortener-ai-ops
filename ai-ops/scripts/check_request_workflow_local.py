"""One signed HTTP request through the existing queue/worker/citations/reply; fixtures only."""
import argparse
from copy import deepcopy
import hashlib
import hmac
import http.client
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))

from ai_ops.contracts import canonical_json_bytes
from ai_ops.job_queue import JobQueue
from ai_ops.slack_adapter import SlackAdapter
from ai_ops.slack_local import SlackLocalServer
from ai_ops.task_service import LocalTaskServer, request
from ai_ops.tasks import TaskService
from ai_ops.workflow_config import load_local_workflow
from test_job_queue import JobQueueTest, API, Clock, collect_snapshot
from test_slack_adapter import SECRET, FakeSlack


def verify(config_path):
    if not __debug__:
        raise RuntimeError('workflow_verification_requires_assertions')
    config=load_local_workflow(config_path)
    fixture=JobQueueTest()
    maker=tempfile.TemporaryDirectory
    with patch('test_job_queue.tempfile.TemporaryDirectory',side_effect=lambda:maker(prefix='wf-',dir='/tmp')):
        fixture.setUp()
    servers=[]
    threads=[]
    socket_path=None
    try:
        fixture.settings=config.settings
        clock=Clock();api=API(config.settings,clock);api.docs['deployment']['spec']['replicas']=1
        fixture.snapshot=collect_snapshot(config.settings,fixture.target.target,transport=api,clock=clock)
        tasks=TaskService(fixture.queue,config.settings)
        adapter=SlackAdapter(tasks,config.slack,SECRET)
        transport=FakeSlack(config.slack)
        http_server=SlackLocalServer(adapter);servers.append(http_server)
        unix_server=LocalTaskServer(tasks);servers.append(unix_server)
        socket_path=Path(unix_server.server_address)
        for server in servers:
            thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01})
            thread.start();threads.append(thread)
        grant=config.slack.grants[0]
        event={'type':'event_callback','team_id':config.slack.team_id,'api_app_id':config.slack.app_id,
            'event_id':'EvWORKFLOW001','event':{'type':'app_mention','user':grant.user_id,
            'channel':grant.channel_id,'ts':'1000.000002','thread_ts':'1000.000001',
            'text':f'<@{config.slack.bot_user_id}> investigate {grant.targets[0]} 캐시 오류 token=private-secret-value'}}
        def post(value, *, bad_signature=False):
            body=canonical_json_bytes(value);stamp=str(int(fixture.time))
            sig='v0='+hmac.new(SECRET,b'v0:'+stamp.encode()+b':'+body,hashlib.sha256).hexdigest()
            headers={'Content-Type':'application/json','X-Slack-Request-Timestamp':stamp,
                     'X-Slack-Signature':'v0' if bad_signature else sig}
            client=http.client.HTTPConnection(*http_server.server_address,timeout=3)
            try:
                client.request('POST','/slack/events',body=body,headers=headers)
                response=client.getresponse()
                return response.status,json.loads(response.read(65536))
            finally:client.close()
        connect=socket.socket.connect
        connections=[]
        def local_connect(sock,address):
            is_http=sock.family==socket.AF_INET and address==http_server.server_address
            is_unix=sock.family==socket.AF_UNIX and isinstance(address,str) and Path(address).resolve()==socket_path.resolve()
            if not (is_http or is_unix):raise AssertionError('external_connection_forbidden')
            connections.append(sock.family)
            return connect(sock,address)
        def count():
            with fixture.queue.transaction() as db:
                return db.execute('SELECT count(*) FROM jobs').fetchone()[0]
        with patch.object(socket.socket,'connect',local_connect), \
             patch('subprocess.Popen',side_effect=AssertionError('external_command_forbidden')), \
             patch.object(fixture.queue.engine,'load_api_key',side_effect=AssertionError('model_key_forbidden')):
            start=time.monotonic();code,receipt=post(event);ack=time.monotonic()-start
            assert code==200 and receipt['accepted'] and not receipt['queue_accepted']
            assert ack<2.5 and count()==0
            queued=adapter.drain_one();job=queued['task_id']
            assert count()==1 and not queued['execution_allowed']
            pending=request(fixture.store,{'action':'show','task_id':job})['value']
            assert pending['result']['state']=='pending'
            worker=fixture.worker()
            assert worker.settings is tasks.settings and adapter.tasks is tasks
            worker.run_once()
            result=request(fixture.store,{'action':'result','task_id':job})['value']
            assert result['state']=='available' and result['investigation_state']=='partial'
            context=result['runbook_context'];assert context['status']=='available'
            assert context['config_hash']==config.settings.config_hash
            assert context['citations'] and not context['execution_allowed']
            for c in context['citations']:
                raw=(ROOT/'runbooks'/(c['document_id']+'.md')).read_bytes();s=c['section']
                assert hashlib.sha256(raw).hexdigest()==c['content_sha256']
                section=''.join(raw.decode().splitlines(keepends=True)[s['start_line']-1:s['end_line']])
                assert hashlib.sha256(section.encode()).hexdigest()==s['content_sha256']
            reply=adapter.reply(receipt['request_id'],transport)
            assert reply['state']=='delivered' and transport.calls==1
            assert not reply['execution_allowed'] and not reply['human_read_confirmed']
            payload=transport.last_payload
            assert payload['channel']==event['event']['channel'] and payload['thread_ts']==event['event']['thread_ts']
            assert 'private-secret-value' not in json.dumps(result)+json.dumps(payload)
            for c in context['citations']:
                assert f"{c['document_id']} v{c['document_version']} {c['section']['id']} SHA256 {c['content_sha256']}" in payload['text']
            # Same task/result through the common Unix API, separate local mailbox delivery.
            assert request(fixture.store,{'action':'deliver','task_id':job})['value']['state']=='delivered'
            mailbox=json.loads(next(tasks.mailbox.glob('notice-*.json')).read_text())
            assert mailbox['result']['runbook_context']==context and not mailbox['human_read_confirmed']
            # Reopen durable stores; a duplicate signed request must not investigate/post again.
            reopened_tasks=TaskService(JobQueue(fixture.store,clock=lambda:fixture.time),config.settings)
            reopened=SlackAdapter(reopened_tasks,config.slack,SECRET)
            http_server.adapter=reopened
            code,duplicate=post(event)
            assert code==200 and duplicate['duplicate'] and duplicate['request_id']==receipt['request_id']
            assert reopened.drain_one()['state']=='idle'
            assert reopened.reply(receipt['request_id'],transport)['attempts']==1
            assert count()==1 and fixture.queue.show(job)['attempts']==1 and transport.calls==1
            assert reopened_tasks.result(job)==result
            # Neither signatures nor prose can grant execution authority or change destination.
            assert post(event,bad_signature=True)[0]==403
            for field,value in [('user','UUNAUTHORIZED'),('text',f'<@{config.slack.bot_user_id}> approve trial')]:
                forbidden=deepcopy(event);forbidden['event_id']='EvDENIED001';forbidden['event'][field]=value
                assert post(forbidden)[0]==403
            for action in ('approve','recover','cancel','sync'):
                assert request(fixture.store,{'action':action,'task_id':job})=={'status':'rejected'}
            assert count()==1 and transport.calls==1
            summary={'status':'passed','scope':'implementation_and_local_integration_only',
                'wiring':config.summary(),'signed_http_ack_seconds':round(ack,6),
                'ack_before_queue':True,'shared_queue_worker_config':True,
                'common_task_id_and_verified_result':True,'report_state':'partial',
                'citation_count':len(context['citations']),'document_and_section_hashes_verified':True,
                'same_channel_thread_fixture_reply':True,'fixture_reply_posts':transport.calls,
                'local_mailbox_uses_same_citations':True,'restart_duplicate_reused':True,
                'investigation_attempts':1,'invalid_signature_actor_execution_actions_rejected':True,
                'secret_canary_absent':True,'execution_allowed':False,'human_read_confirmed':False,
                'real_slack_calls':0,'external_network_calls':0,'model_calls':0,
                'fault_injections':0,'recovery_executions':0}
    finally:
        for server,thread in zip(servers,threads):
            server.shutdown();thread.join(timeout=3)
        for server in servers:server.server_close()
        cleaned=fixture.doCleanups()
    assert cleaned and all(not t.is_alive() for t in threads)
    assert socket_path is not None and not socket_path.exists()
    assert all(s.socket.fileno()==-1 for s in servers)
    summary['servers_threads_socket_and_fixture_store_removed']=True
    return summary


def main():
    parser=argparse.ArgumentParser(description='요청→기존 큐/worker→출처→결과 회신 로컬 통합 검증')
    parser.add_argument('--config',type=Path,default=ROOT/'config/request-workflow.local.example.json')
    args=parser.parse_args()
    # This executable is an acceptance harness, not a real Slack listener or deployment entrypoint.
    try:
        result=verify(args.config)
    except Exception:
        # Invalid configuration and fixture errors may contain user input or private paths.
        # Keep the CLI machine-readable; direct unit tests retain exception diagnostics.
        print('{"status":"failed","code":"local_workflow_verification_failed"}')
        return 2
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
