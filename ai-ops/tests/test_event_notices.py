"""WF-03 auth/arrival/receipt tests use synthetic credentials and loopback TLS only."""
import copy
import http.client
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import ssl
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from ai_ops.alert_receiver import receiver_server
from ai_ops.event_notices import EventInbox
from ai_ops.event_store import EventStore
from ai_ops.job_queue import QueueFull, RETENTION_SECONDS
import test_job_queue as fixtures


class EventFixture:
    def setUp(self):
        self.f=fixtures.JobQueueTest();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.q=self.f.queue
        self.sources={'lab-seoul':{'source_id':'am-lab','cluster_id':self.f.target.target.cluster_id}}
        self.store=EventStore(self.q,self.f.settings,sources=self.sources)
        self.inbox=EventInbox(self.store,source_alias='lab-seoul')

    def alert(self,**changes):
        item={'labels':{**self.f.target.target.model_dump(),'alertname':'HighErrorRate','ai_ops':'enabled'},
              'startsAt':'1970-01-01T00:16:40.123456789Z','status':'firing','fingerprint':'0123456789abcdef',
              'annotations':{'summary':'token=synthetic-private'}}
        item.update(changes);return item

    def body(self,item=None,**changes):
        value={'version':'4','receiver':'ai-events','status':'firing','alerts':[item or self.alert()]}
        value.update(changes);return json.dumps(value).encode()

    def count(self,table):
        with self.q.transaction() as db:return db.execute('SELECT count(*) FROM '+table).fetchone()[0]


class EventNoticesTest(EventFixture, unittest.TestCase):
    def test_notice_arrives_before_intake_pending_then_registered_unknown_delivery(self):
        ref='al1/lab-seoul/0123456789abcdef/1970-01-01T00:16:40.123456789Z'
        before=self.store.reference_state(ref)
        self.assertEqual(before['registration'],'pending');self.assertEqual(self.count('events'),0)
        receipt=self.inbox.accept(self.body())
        after=self.store.reference_state(ref)
        self.assertEqual(after['registration'],'registered')
        self.assertEqual(before['event_id'],receipt['event_ids'][0])
        self.assertEqual(after['notification'],'unknown')
        self.assertFalse(receipt['investigation_started']);self.assertFalse(receipt['execution_allowed'])
        self.assertEqual(self.count('jobs'),0)

    def test_intake_before_notice_retransmit_and_no_notice_sender(self):
        first=self.inbox.accept(self.body());again=self.inbox.accept(self.body())
        self.assertEqual(first,again);self.assertEqual(self.count('events'),1)
        self.assertEqual(self.count('event_requests'),0)
        self.assertEqual(self.store.reference_state(first['event_ids'][0])['notification'],'unknown')
        self.assertNotIn('synthetic-private',json.dumps(first))

    def test_reference_expired_unknown_source_invalid_no_side_effect(self):
        event=self.inbox.accept(self.body())['event_ids'][0]
        self.f.time+=RETENTION_SECONDS+1
        self.assertEqual(self.store.reference_state(event)['registration'],'expired')
        with self.assertRaises(ValueError): self.store.reference_state('al1/foreign/0123456789abcdef/1970-01-01T00:16:40Z')
        with self.assertRaises(ValueError): self.store.reference_state('https://slack.invalid/arbitrary')
        self.assertEqual(self.count('jobs'),0)

    def test_resolution_keeps_occurrence_reference(self):
        event=self.inbox.accept(self.body())['event_ids'][0]
        receipt=self.inbox.accept(self.body(self.alert(status='resolved',endsAt='1970-01-01T00:16:41Z')))
        self.assertEqual(receipt['event_ids'],[event]);self.assertEqual(self.store.reference_state(event)['alert_status'],'resolved')
        self.assertEqual(self.store.reference_state(event)['notification'],'unknown')

    def test_server_source_and_cluster_are_not_payload_authority(self):
        value=self.alert();value['labels']['cluster_id']='foreign'
        with self.assertRaises(ValueError):self.inbox.accept(self.body(value))
        with self.assertRaises(ValueError):EventInbox(self.store,source_alias='foreign')
        with self.assertRaises(ValueError):self.inbox.accept(self.body(receiver='ai-investigation'))
        self.assertEqual(self.count('events'),0)


class EventTLSTest(EventFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certificates=tempfile.TemporaryDirectory();cls.addClassCleanup(cls.certificates.cleanup)
        root=Path(cls.certificates.name)
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=localhost',
            '-addext','subjectAltName=DNS:localhost','-keyout',str(root/'tls.key'),'-out',str(root/'tls.crt')],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

    def setUp(self):
        super().setUp()
        self.identity=self.f.root/'identity';self.identity.mkdir(mode=0o700)
        for name in ('tls.key','tls.crt'):
            (self.identity/name).write_bytes((Path(self.certificates.name)/name).read_bytes());(self.identity/name).chmod(0o600)
        self.token='synthetic-event-token-'+'x'*40
        (self.identity/'token').write_text(self.token);(self.identity/'token').chmod(0o600)
        self.context=ssl.create_default_context(cafile=str(self.identity/'tls.crt'))
        ctx=receiver_server(self.inbox,self.identity,address=('127.0.0.1',0))
        self.server=ctx.__enter__();self.addCleanup(ctx.__exit__,None,None,None)

    def send(self,*,token=None,path='/v1/events',raw=None):
        conn=http.client.HTTPSConnection('localhost',self.server.server_port,context=self.context,timeout=4)
        try:
            conn.request('POST',path,body=raw or self.body(),headers={'Authorization':'Bearer '+(self.token if token is None else token),'Content-Type':'application/json'})
            resp=conn.getresponse();return resp.status,json.loads(resp.read())
        finally:conn.close()

    def test_actual_tls_auth_commit_and_endpoint_isolation(self):
        self.assertEqual(self.send(token='wrong')[0],401);self.assertEqual(self.count('events'),0)
        self.assertEqual(self.send(path='/v1/alerts')[0],404)
        code,receipt=self.send();self.assertEqual(code,202)
        self.assertEqual(self.store.reference_state(receipt['event_ids'][0])['registration'],'registered')
        self.assertEqual(self.count('jobs'),0)
        (self.identity/'token').write_text('replacement-token-'+'z'*50)
        self.assertEqual(self.send()[0],401)
        self.assertEqual(self.send(token='replacement-token-'+'z'*50)[0],202)

    def test_storage_failure_no_false_ack_or_delivery_claim(self):
        for error in (sqlite3.OperationalError('secret-storage'),QueueFull('secret-full')):
            with patch.object(self.store,'accept',side_effect=error):
                code,receipt=self.send()
                self.assertEqual(code,503);self.assertNotIn('event_ids',receipt);self.assertNotIn('secret',json.dumps(receipt))
        self.assertEqual(self.count('events'),0)

    def test_wrong_cluster_truncation_rate_limit_no_registration(self):
        item=self.alert();item['labels']['cluster_id']='foreign'
        self.assertEqual(self.send(raw=self.body(item))[0],400)
        self.assertEqual(self.send(raw=self.body(truncatedAlerts=1))[0],400)
        self.server.calls=60;self.assertEqual(self.send()[0],429)
        self.assertEqual(self.count('events'),0)


@unittest.skipUnless(os.environ.get('WF03_PLATFORM_ROOT') and os.environ.get('WF03_AMTOOL'),'explicit platform source/amtool required')
class TemplateReferenceTest(EventFixture, unittest.TestCase):
    def test_real_template_reference_resolves_same_authenticated_event(self):
        platform=Path(os.environ['WF03_PLATFORM_ROOT'])
        path=platform/'cluster-addons/prometheus/wf03/prepare.py'
        spec=importlib.util.spec_from_file_location('wf03_prepare',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        item=self.alert(startsAt='1970-01-01T09:16:40.123456789+09:00')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);template=root/'notice.tmpl';template.write_text(module.notice_template('lab-seoul','#ai-ops'))
            data=root/'data.json';data.write_text(json.dumps({'status':'firing','receiver':'slack-notifications','alerts':[item]}))
            result=subprocess.check_output([os.environ['WF03_AMTOOL'],'template','render','--template.glob='+str(template),
                '--template.text={{ template "wf03.notice" . }}','--template.data='+str(data)],text=True,timeout=10)
        ref=next(line.split(': ',1)[1] for line in result.splitlines() if line.startswith('사건 참조: '))
        pending=self.store.reference_state(ref)
        event=self.inbox.accept(self.body(item))['event_ids'][0]
        self.assertEqual(pending['event_id'],event);self.assertEqual(self.store.resolve_reference(ref),event)
        self.assertEqual(ref,'al1/lab-seoul/0123456789abcdef/1970-01-01T00:16:40.123456789Z')
        self.assertEqual(self.count('jobs'),0)


if __name__=='__main__':unittest.main()
