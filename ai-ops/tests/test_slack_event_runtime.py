"""WF-04 ingress/relay factory and ASGI integration. All sockets/model calls blocked."""
import asyncio
from copy import deepcopy
import hashlib
import hmac
import socket
import unittest
from unittest.mock import patch

from ai_ops.contracts import canonical_json_bytes
from ai_ops.slack_server import make_ingress
from ai_ops.slack_runtime import SlackRuntime, SlackWebConfig
from test_slack_web import WebFixture, SIGNING, invoke


class EventRuntimeTest(WebFixture,unittest.TestCase):
    def setUp(self):
        super().setUp()
        from ai_ops.workflow_config import load_local_workflow
        from ai_ops.tasks import TaskService
        from test_job_queue import ROOT, Clock, API, collect_snapshot
        settings=load_local_workflow(ROOT/'config/request-workflow.local.example.json').settings
        self.fixture.settings=settings
        clock=Clock();api=API(settings,clock);api.docs['deployment']['spec']['replicas']=1
        self.fixture.snapshot=collect_snapshot(settings,self.fixture.target.target,transport=api,clock=clock)
        self.tasks=TaskService(self.queue,settings)
        self.sources={'lab-seoul':{'source_id':'am-lab','cluster_id':self.fixture.target.target.cluster_id}}
        self.config=SlackWebConfig(enabled=True,bot_token_file=str(self.token_file),
            signing_secret_file=str(self.secret_file),event_sources=self.sources)
        self.runtime=self.make_runtime()
        self.eid=self.runtime.adapter.events.ingest('lab-seoul',{
            'labels':{**self.fixture.target.target.model_dump(),'alertname':'CacheHitRateLow'},
            'status':'firing','fingerprint':'0123456789abcdef','startsAt':'1970-01-01T00:16:40Z'})
        self.serial=0

    def make_runtime(self):
        return SlackRuntime(self.tasks,self.policy,self.config,connection_factory=self.wire.connect,
                            monotonic=lambda:self.now)

    def receive_event(self, app, command='investigate-event'):
        self.serial+=1
        event=deepcopy(self.event);event['event_id']=f'EvRUNTIME{self.serial:04}'
        event['event']['text']=f'<@{self.policy.bot_user_id}> {command} {self.eid}'
        raw=canonical_json_bytes(event);stamp=str(int(self.fixture.time))
        signature='v0='+hmac.new(SIGNING,b'v0:'+stamp.encode()+b':'+raw,hashlib.sha256).hexdigest()
        return asyncio.run(invoke(app,{'Content-Type':'application/json','X-Slack-Request-Timestamp':stamp,
                                      'X-Slack-Signature':signature},raw))

    def test_asgi_event_receipt_existing_worker_result_and_restarted_relay(self):
        with (patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden')),
              patch.object(self.queue.engine,'load_api_key',side_effect=AssertionError('model forbidden'))):
            app=make_ingress(self.tasks,self.policy,self.config)
            status,receipt=self.receive_event(app)
            self.assertEqual(status,200);self.assertFalse(receipt['queue_accepted'])
            self.assertEqual(self.wire.calls,[])
            self.assertEqual(self.runtime.relay_once()['admission']['state'],'queued')
            self.fixture.worker().run_once()
            self.assertEqual(self.runtime.relay_once()['reply']['state'],'delivered')
            self.assertIn(self.eid,self.wire.posts()[0]['body']['text'])
            self.assertIn('실제 자료 범위:',self.wire.posts()[0]['body']['text'])
            job=self.runtime.adapter._row(receipt['request_id'])['job_id']
            citations=self.tasks.result(job)['runbook_context']['citations']
            self.assertTrue(citations)
            for citation in citations:
                for value in (citation['document_id'],citation['content_sha256'],citation['section']['id']):
                    self.assertIn(value,self.wire.posts()[0]['body']['text'])
            self.make_runtime().relay_once()
            self.assertEqual(len(self.wire.posts()),1)

    def test_status_ready_without_job_is_delivered_by_real_relay_loop(self):
        with patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden')):
            status,_=self.receive_event(self.runtime.app,'status-event')
            self.assertEqual(status,200)
            reply=self.runtime.relay_once()['reply']
            self.assertEqual(reply['state'],'delivered')
            self.assertIn('연결된 조사 작업 없음',self.wire.posts()[0]['body']['text'])
            with self.queue.transaction() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
            self.make_runtime().relay_once();self.assertEqual(len(self.wire.posts()),1)

    def test_event_ingress_never_reads_bot_token(self):
        self.token_file.unlink()
        with patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden')):
            app=make_ingress(self.tasks,self.policy,self.config)
            self.assertEqual(self.receive_event(app)[0],200)
        self.assertEqual(self.wire.calls,[])


if __name__=='__main__':unittest.main()
