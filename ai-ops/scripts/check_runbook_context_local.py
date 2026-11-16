"""기존 소켓/큐/worker/결과/가짜 Slack을 거친 출처 인용을 로컬에서 검증한다."""
import hashlib
import json
from pathlib import Path
import sys
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tests'))
from test_runbook_context import RunbookContextTest
from ai_ops.task_service import LocalTaskServer, request


def main():
    fixture=RunbookContextTest();fixture.setUp()
    server=None;thread=None
    try:
        with patch('socket.create_connection',side_effect=AssertionError('external_network_forbidden')), \
             patch('subprocess.Popen',side_effect=AssertionError('external_command_forbidden')), \
             patch.object(fixture.f.queue.engine,'load_api_key',side_effect=AssertionError('model_key_forbidden')):
            server=LocalTaskServer(fixture.tasks)
            thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.01});thread.start()
            receipt=request(fixture.f.store,{'action':'submit','request':fixture.raw})['value']
            assert not receipt['analysis_completed'] and not receipt['execution_allowed']
            job=receipt['task_id'];fixture.f.worker().run_once()
            result=request(fixture.f.store,{'action':'result','task_id':job})['value']
            context=result['runbook_context'];assert context['status']=='available'
            assert not context['execution_allowed'] and not context['external_model_transfer']
            for citation in context['citations']:
                assert citation['document_id']=='cache'
                raw=(fixture.books/'cache.md').read_bytes();section=citation['section']
                assert hashlib.sha256(raw).hexdigest()==citation['content_sha256']
                assert hashlib.sha256(''.join(raw.decode().splitlines(keepends=True)[section['start_line']-1:section['end_line']]).encode()).hexdigest()==section['content_sha256']
            notice=request(fixture.f.store,{'action':'deliver','task_id':job})['value']
            assert notice['state']=='delivered'
            mailbox=json.loads(next(fixture.tasks.mailbox.glob('notice-*.json')).read_text())
            assert mailbox['result']['runbook_context']==context and not mailbox['human_read_confirmed']
            duplicate=request(fixture.f.store,{'action':'submit','request':fixture.raw})['value']
            assert duplicate['duplicate'] and duplicate['task_id']==job
            assert fixture.f.queue.show(job)['attempts']==1
            adapter,transport,slack_receipt,queued=fixture.slack()
            reply=adapter.reply(slack_receipt['request_id'],transport)
            assert reply['state']=='delivered' and transport.calls==1
            assert '참고: cache v1' in transport.last_payload['text']
            assert not reply['execution_allowed'] and not reply['human_read_confirmed']
            fixture.policy_path.unlink()
            assert fixture.tasks.result(job)['runbook_context']['status']=='withheld'
            assert not fixture.tasks.result(job)['runbook_context']['citations']
            summary={'status':'passed','existing_unix_queue_worker_result_mailbox':True,
                'signed_slack_fixture_reply_with_citation':True,'allowed_document_ids':['cache'],
                'provenance_recomputed_citations':len(context['citations']),
                'duplicate_reuses_job':True,'investigation_attempts':1,'revocation_withholds_sources':True,
                'model_calls':0,'real_slack_calls':0,'external_network_calls':0,'real_credentials_read_or_issued':0,
                'fault_injections':0,'recovery_executions':0,'execution_allowed':False}
    finally:
        if server is not None:
            if thread is not None:server.shutdown();thread.join(timeout=5)
            server.server_close()
        assert fixture.doCleanups(),'fixture_cleanup_failed'
    summary['socket_closed']=not Path(server.server_address).exists()
    assert summary['socket_closed'] and not thread.is_alive()
    print(json.dumps(summary,ensure_ascii=False))


if __name__=='__main__':main()
