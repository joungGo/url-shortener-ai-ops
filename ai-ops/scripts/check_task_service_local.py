"""가짜 수집 API와 실제 로컬 socket/큐/worker/보고서/알림 경로를 연결한다."""

from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))

from ai_ops.b_contracts import load_b_settings
from ai_ops.job_queue import JobQueue
from ai_ops.live_collectors import collect_snapshot
from ai_ops.task_service import LocalTaskServer, request
from ai_ops.tasks import TaskService
from ai_ops.worker import Worker
from test_live_collectors import API, Clock


def main():
    with tempfile.TemporaryDirectory(prefix='task-flow-', dir='/private/tmp' if sys.platform=='darwin' else '/tmp') as directory:
        with patch.dict(os.environ, {'AI_OPS_FILES_ROOT': directory}):
            settings = load_b_settings(ROOT/'config/targets.practice.example.json',
                ROOT/'config/b-runtime.example.json', ROOT/'config/metric-queries.json')
            queue = JobQueue(Path(directory)/'store')
            service = TaskService(queue, settings)
            clock = Clock(); api = API(settings, clock)
            api.docs['deployment']['spec']['replicas'] = 1
            snapshot = collect_snapshot(settings, settings.bindings.bindings[0].target, transport=api, clock=clock)
            with LocalTaskServer(service) as server:
                thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
                thread.start()
                try:
                    with patch('socket.create_connection', side_effect=AssertionError('external_network_forbidden')), \
                         patch.object(queue.engine, 'load_api_key', side_effect=AssertionError('model_key_forbidden')):
                        raw = {'action':'submit','request':{'request_id':'local-demo-1','conversation_id':'demo-thread',
                               'target':settings.bindings.bindings[0].alias,'question':'현재 상태 확인'}}
                        accepted = request(queue.root,raw)['value']; job=accepted['task_id']
                        assert accepted['accepted'] and not accepted['analysis_completed']
                        assert request(queue.root,{'action':'show','task_id':job})['value']['result']['state']=='pending'
                        with redirect_stderr(io.StringIO()):
                            Worker(queue,settings,collector=lambda *a,**kw:snapshot).run_once()
                        result=request(queue.root,{'action':'result','task_id':job})['value']
                        notice=request(queue.root,{'action':'deliver','task_id':job})['value']
                        assert result['state']=='available' and result['investigation_state']=='partial'
                        assert notice['state']=='delivered'
                        assert request(queue.root,raw)['value']['duplicate']
                        reopened=TaskService(JobQueue(queue.root),settings)
                        assert reopened.deliver(job)['attempts']==1
                        assert reopened.show(job)['recovery']['execution_allowed'] is False
                        assert len(list(service.mailbox.glob('notice-*.json')))==1
                finally:
                    server.shutdown();thread.join(timeout=3)
                path=Path(server.server_address)
            assert not path.exists()
    print(json.dumps({'status':'passed','transport':'local_unix_socket','collector':'fixture_only',
        'queue_and_worker':'existing_implementation','report':'verified_rules_partial',
        'common_task_lookup':True,'duplicate_reused':True,'restart_reused':True,
        'local_notice_files':1,'human_read_confirmed':False,'socket_removed':True,
        'external_network_calls':0,'model_calls':0,'fault_injections':0,'recovery_executions':0}))


if __name__=='__main__':main()
