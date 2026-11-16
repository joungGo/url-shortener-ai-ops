"""대상별 출처 인용과 기존 큐/worker/결과/로컬 회신의 연결 경계."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import threading
import unittest
from unittest.mock import patch

from ai_ops import replay_tools
from ai_ops.b_contracts import load_b_settings
from ai_ops.contracts import canonical_json_bytes, sha256_json
from ai_ops.incidents import _atomic_write
from ai_ops.job_queue import JobQueue, RETENTION_SECONDS
from ai_ops.retention import QueueRetention
from ai_ops.runbook_context import RunbookPolicy, Context, render_context, validate_saved
from ai_ops.task_service import LocalTaskServer, request
from ai_ops.tasks import TaskService
from ai_ops.worker import Worker
from ai_ops.slack_adapter import SlackAdapter, SlackPolicy
import test_job_queue as fixtures
from test_job_queue import API, Clock, collect_snapshot, ROOT
from test_slack_adapter import FakeSlack, SECRET


class RunbookContextTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        maker = tempfile.TemporaryDirectory
        f = self.f = fixtures.JobQueueTest()
        with patch.object(fixtures.tempfile, 'TemporaryDirectory', side_effect=lambda: maker(dir='/tmp', prefix='rbc-')):
            f.setUp()
        self.addCleanup(f.doCleanups)
        self.books = f.root/'runbooks';shutil.copytree(ROOT/'runbooks', self.books)
        patched = patch.object(replay_tools, 'RUNBOOK_ROOT', self.books);patched.start();self.addCleanup(patched.stop)
        self.policy_path = f.root/'runbook-policy.json'
        self.raw_policy = {'schema_version':1,
            'registry_sha256':hashlib.sha256((self.books/'index.yaml').read_bytes()).hexdigest(),
            'metadata_sha256':hashlib.sha256((self.books/'search-metadata.json').read_bytes()).hexdigest(),
            'targets':[{'alias':f.target.alias,'target':f.target.target.model_dump(mode='json'),
                        'document_ids':['cache']}]}
        self.configure()
        self.raw = {'request_id':'runbook-1','conversation_id':'thread-1',
                    'target':f.target.alias,'question':'캐시 오류 token=private-secret-value'}

    def configure(self):
        self.policy_path.write_bytes(canonical_json_bytes(self.raw_policy))
        self.settings = load_b_settings(ROOT/'config/targets.practice.example.json', ROOT/'config/b-runtime.example.json',
            ROOT/'config/metric-queries.json', runbook_policy=self.policy_path)
        self.f.settings = self.settings
        clock=Clock();api=API(self.settings,clock);api.docs['deployment']['spec']['replicas']=1
        self.f.snapshot=collect_snapshot(self.settings,self.f.target.target,transport=api,clock=clock)
        self.tasks=TaskService(self.f.queue,self.settings)

    def complete(self):
        receipt=self.tasks.submit(self.raw)
        with patch('socket.create_connection',side_effect=AssertionError('external_forbidden')), \
             patch.object(self.f.queue.engine,'load_api_key',side_effect=AssertionError('model_key_forbidden')):
            result=self.f.worker().run_once()
        self.assertEqual(result['state'],'partial')
        return receipt['task_id']

    def record(self,job):
        view=self.f.queue.show(job)
        return self.f.queue.engine.show(view['incident_id'],view['run_id'])

    def context_path(self,job):
        record=self.record(job)
        return self.f.queue.root/record.incident_id/record.run_id/'runbook-context.json'

    def test_end_to_end_saved_provenance_and_separate_observation(self):
        job=self.complete();value=self.tasks.result(job);context=value['runbook_context'];record=self.record(job)
        self.assertEqual(context['status'],'available')
        self.assertEqual({c['document_id'] for c in context['citations']},{'cache'})
        self.assertEqual(context,record.runbook_context)
        self.assertEqual(context['config_hash'],self.settings.config_hash)
        self.assertEqual(context['report_hash'],sha256_json(record.report.model_dump(mode='json')))
        self.assertFalse(context['execution_allowed']);self.assertFalse(context['external_model_transfer'])
        self.assertNotIn('private-secret-value',json.dumps(value))
        self.assertIn('런북 참고 자료',record.markdown)
        self.assertFalse(any(eid.startswith('rb-') for eid in value['evidence_ids']))
        for c in context['citations']:
            raw=(self.books/(c['document_id']+'.md')).read_bytes();s=c['section']
            self.assertEqual(c['content_sha256'],hashlib.sha256(raw).hexdigest())
            self.assertEqual(s['content_sha256'],hashlib.sha256(''.join(raw.decode().splitlines(keepends=True)[s['start_line']-1:s['end_line']]).encode()).hexdigest())
            self.assertEqual(c['document_version'],1)
        self.assertEqual(self.context_path(job).stat().st_mode & 0o777,0o600)

    def test_reopened_result_and_mailbox_use_same_citations_without_analyzing_again(self):
        job=self.complete();expected=self.tasks.result(job)['runbook_context']
        reopened=TaskService(JobQueue(self.f.store,clock=lambda:self.f.time),self.settings)
        with patch.object(self.f.queue.engine,'investigate_snapshot',side_effect=AssertionError('no_reanalysis')):
            self.assertEqual(reopened.result(job)['runbook_context'],expected)
            self.assertEqual(reopened.deliver(job)['state'],'delivered')
        payload=json.loads(next(reopened.mailbox.glob('notice-*.json')).read_text())
        self.assertEqual(payload['result']['runbook_context'],expected)
        self.assertFalse(payload['human_read_confirmed'])
        self.assertEqual(self.f.queue.show(job)['attempts'],1)

    def test_policy_omission_preserves_old_settings_hash_and_result_shape(self):
        self.f.settings=load_b_settings(ROOT/'config/targets.practice.example.json',ROOT/'config/b-runtime.example.json',ROOT/'config/metric-queries.json')
        self.assertIsNone(self.f.settings.runbooks)
        self.assertNotEqual(self.f.settings.config_hash,self.settings.config_hash)
        clock=Clock();api=API(self.f.settings,clock);api.docs['deployment']['spec']['replicas']=1
        self.f.snapshot=collect_snapshot(self.f.settings,self.f.target.target,transport=api,clock=clock)
        self.tasks=TaskService(self.f.queue,self.f.settings)
        job=self.complete();self.assertNotIn('runbook_context',self.tasks.result(job))
        self.assertIsNone(self.record(job).runbook_context)

    def test_missing_policy_at_result_withholds_old_sources(self):
        job=self.complete()
        old=replace(self.settings,runbooks=None)
        context=TaskService(self.f.queue,old).result(job)['runbook_context']
        self.assertEqual(context['status'],'withheld');self.assertEqual(context['citations'],[])

    def test_ungranted_document_cannot_be_requested_by_question(self):
        self.raw['question']='database deployment ImagePullBackOff DB 지연 document_ids=database'
        job=self.complete();citations=self.tasks.result(job)['runbook_context']['citations']
        self.assertTrue(all(c['document_id']=='cache' for c in citations))

    def test_request_cannot_supply_policy_ids_or_filters(self):
        for extra in [{'runbook_policy':str(self.policy_path)},{'document_ids':['database']},
                      {'service':'postgresql'},{'runbook_context':{}},{'execution_allowed':True}]:
            with self.subTest(extra=extra),self.assertRaises(ValueError):self.tasks.submit({**self.raw,**extra})

    def test_no_grant_and_explicit_empty_grant_are_not_allowed(self):
        for index,targets in enumerate([[],[{**self.raw_policy['targets'][0],'document_ids':[]}]]):
            self.raw_policy['targets']=targets;self.configure();self.raw['request_id']=f'none-{index}'
            job=self.complete();value=self.tasks.result(job)['runbook_context']
            self.assertEqual(value['status'],'not_allowed');self.assertEqual(value['citations'],[])

    def test_policy_scope_identity_duplicates_and_unknown_documents_rejected(self):
        baseline=deepcopy(self.raw_policy)
        variants=[]
        wrong=deepcopy(baseline);wrong['targets'][0]['target']['namespace']='foreign';variants.append(wrong)
        wrong=deepcopy(baseline);wrong['targets'][0]['alias']='foreign';variants.append(wrong)
        wrong=deepcopy(baseline);wrong['targets'].append(wrong['targets'][0]);variants.append(wrong)
        wrong=deepcopy(baseline);wrong['targets'][0]['document_ids']=['cache','cache'];variants.append(wrong)
        wrong=deepcopy(baseline);wrong['targets'][0]['document_ids']=['unknown'];variants.append(wrong)
        wrong=deepcopy(baseline);wrong['targets'][0]['document_ids']=['../database'];variants.append(wrong)
        for value in variants:
            self.policy_path.write_bytes(canonical_json_bytes(value))
            with self.subTest(value=value),self.assertRaises(ValueError):RunbookPolicy(self.policy_path,self.settings.bindings)

    def test_changed_policy_holds_queued_job_before_collection(self):
        job=self.tasks.submit(self.raw)['task_id']
        self.raw_policy['targets'][0]['document_ids']=['database'];self.configure()
        with patch('socket.create_connection',side_effect=AssertionError('network_forbidden')):
            result=self.f.worker().run_once()
        self.assertEqual(result['state'],'needs_review');self.assertEqual(result['reason'],'config_changed')

    def test_revocation_in_same_process_withholds_saved_reference(self):
        job=self.complete();self.raw_policy['targets'][0]['document_ids']=[]
        self.policy_path.write_bytes(canonical_json_bytes(self.raw_policy))
        result=self.tasks.result(job)
        self.assertEqual(result['state'],'available')
        self.assertEqual(result['runbook_context']['status'],'withheld')
        self.assertEqual(result['runbook_context']['citations'],[])

    def test_catalog_and_document_changes_never_substitute_fresh_sources(self):
        job=self.complete();saved=self.tasks.result(job)['runbook_context']
        for name in ['cache.md','index.yaml','search-metadata.json']:
            path=self.books/name;raw=path.read_bytes();path.write_bytes(raw+b'\n')
            with self.subTest(name=name):self.assertEqual(self.tasks.result(job)['runbook_context']['status'],'withheld')
            path.write_bytes(raw)
        self.assertEqual(self.tasks.result(job)['runbook_context'],saved)

    def test_deleted_policy_does_not_remove_core_result(self):
        job=self.complete();self.policy_path.unlink()
        value=self.tasks.result(job)
        self.assertEqual(value['state'],'available');self.assertEqual(value['runbook_context']['status'],'withheld')

    def test_unavailable_search_does_not_invent_sources(self):
        job=self.complete();record=self.record(job)
        request_changed=record.request.model_copy(update={'question':'독립적으로일치없음'})
        report_changed=record.report.model_copy(update={'facts':[]})
        context=self.settings.runbooks.build(request_changed,record.bundle,report_changed,self.settings)
        self.assertEqual(context['status'],'unavailable');self.assertEqual(context['citations'],[])

    def test_search_corruption_records_reference_rejection_without_changing_diagnosis(self):
        (self.books/'cache.md').write_text('tampered')
        job=self.complete();result=self.tasks.result(job)
        self.assertEqual(result['investigation_state'],'partial')
        self.assertEqual(self.record(job).runbook_context['status'],'rejected')
        self.assertEqual(result['runbook_context']['citations'],[])

    def test_saved_context_hash_tampering_is_rejected(self):
        job=self.complete();path=self.context_path(job);value=json.loads(path.read_text());value['citations']=[]
        path.write_bytes(canonical_json_bytes(value))
        with self.assertRaises(ValueError):self.tasks.result(job)

    def test_recomputed_context_hash_still_cannot_forge_citation(self):
        job=self.complete();path=self.context_path(job);value=json.loads(path.read_text())
        value['citations'][0]['excerpt']['text']='forged reference'
        path.write_bytes(canonical_json_bytes(value))
        metadata=path.parent/'metadata.json';body=json.loads(metadata.read_text());body['runbook_context_hash']=sha256_json(value)
        metadata.write_bytes(canonical_json_bytes(body))
        self.assertEqual(self.tasks.result(job)['runbook_context']['status'],'withheld')

    def test_cross_run_context_binding_is_rejected(self):
        first=self.complete();context=self.record(first).runbook_context
        self.raw['request_id']='runbook-2';second=self.complete();record=self.record(second)
        with self.assertRaises(ValueError):validate_saved(context,record.request,record.bundle,record.report)

    def test_search_changed_mid_build_is_rejected_without_quotes(self):
        original=self.settings.runbooks._active;calls=[]
        def changing(registry):
            result=original(registry);calls.append(1)
            if len(calls)==1:
                with (self.books/'search-metadata.json').open('a') as stream:stream.write(' ')
            return result
        with patch.object(self.settings.runbooks,'_active',side_effect=changing):job=self.complete()
        context=self.record(job).runbook_context
        self.assertEqual(context['status'],'rejected');self.assertEqual(context['citations'],[])

    def test_cancel_and_expiry_never_return_reference_sources(self):
        job=self.tasks.submit(self.raw)['task_id'];self.f.queue.cancel(job)
        self.assertNotIn('runbook_context',self.tasks.result(job))
        self.assertIsNone(self.f.worker().run_once())
        self.raw['request_id']='expiry';job=self.complete();self.f.time+=RETENTION_SECONDS
        self.assertEqual(self.tasks.result(job)['state'],'expired')
        self.assertNotIn('runbook_context',self.tasks.result(job))

    def test_retention_includes_context_sidecar(self):
        job=self.complete();path=self.context_path(job);self.tasks.deliver(job)
        self.f.time+=RETENTION_SECONDS+1
        result=QueueRetention(self.f.queue).run(apply=True,job_id=job)
        self.assertFalse(path.exists());self.assertEqual(self.f.queue.show(job)['state'],'purged')
        self.assertNotIn('캐시',json.dumps(result,ensure_ascii=False))

    def test_markdown_quotes_are_escaped_and_reference_only(self):
        job=self.complete();context=self.record(job).runbook_context
        context['citations'][0]['excerpt']['text']='[run](https://bad.example) <script> @here'
        text=render_context(context)
        self.assertNotIn('https://bad.example',text);self.assertNotIn('<script>',text)
        self.assertIn('관측 증거·승인·실행 결과 아님',text)

    def test_multiple_document_grants_are_bounded_and_document_versions_preserved(self):
        self.raw_policy['targets'][0]['document_ids']=['cache','database','deployment'];self.configure()
        self.raw['question']='캐시 DB 배포 ImagePullBackOff 오류'
        job=self.complete();citations=self.tasks.result(job)['runbook_context']['citations']
        self.assertEqual({c['document_id'] for c in citations},{'cache','database','deployment'})
        self.assertLessEqual(len(citations),3)

    def test_two_registered_targets_have_independent_document_scopes(self):
        bindings=json.loads((ROOT/'config/targets.practice.example.json').read_text())
        second=deepcopy(bindings['bindings'][0]);second['alias']='second-practice'
        second['target']['namespace']='second-shortener'
        bindings['bindings'].append(second)
        binding_path=self.f.root/'two-targets.json';binding_path.write_bytes(canonical_json_bytes(bindings))
        self.raw_policy['targets'].append({'alias':second['alias'],'target':second['target'],'document_ids':['database']})
        self.policy_path.write_bytes(canonical_json_bytes(self.raw_policy))
        self.settings=load_b_settings(binding_path,ROOT/'config/b-runtime.example.json',ROOT/'config/metric-queries.json',runbook_policy=self.policy_path)
        self.f.settings=self.settings;self.tasks=TaskService(self.f.queue,self.settings)
        clock=Clock();api=API(self.settings,clock);api.docs['deployment']['spec']['replicas']=1
        self.f.snapshot=collect_snapshot(self.settings,self.f.target.target,transport=api,clock=clock)
        self.raw['question']='캐시 DB 지연';first=self.complete()
        self.assertEqual({c['document_id'] for c in self.tasks.result(first)['runbook_context']['citations']},{'cache'})
        binding=self.settings.bindings.bindings[1];clock=Clock();api=API(self.settings,clock)
        api.docs['deployment']['spec']['replicas']=1
        for value in api.docs.values():
            if value.get('metadata',{}).get('namespace')=='url-shortener':value['metadata']['namespace']=binding.target.namespace
        api.docs['app']['spec']['destination']['namespace']=binding.target.namespace
        self.f.snapshot=collect_snapshot(self.settings,binding.target,transport=api,clock=clock)
        self.raw.update(request_id='second-target',target=second['alias'])
        job=self.complete()
        self.assertEqual({c['document_id'] for c in self.tasks.result(job)['runbook_context']['citations']},{'database'})

    def test_metadata_only_reference_keeps_explicit_match_kind(self):
        path=self.books/'search-metadata.json';body=json.loads(path.read_text())
        body['documents'][0]['keywords'].append('사용자정의증상')
        path.write_bytes(canonical_json_bytes(body))
        self.raw_policy['metadata_sha256']=hashlib.sha256(path.read_bytes()).hexdigest();self.configure()
        self.raw['question']='사용자정의증상';job=self.complete()
        citation=self.tasks.result(job)['runbook_context']['citations'][0]
        self.assertEqual(citation['match_kind'],'metadata_keyword')
        self.assertEqual(citation['section']['start_line'],1)

    def test_existing_queue_lookup_uses_same_context_without_creating_reply_route(self):
        job,_=self.f.queue.submit(self.settings,target=self.f.target.alias,question='캐시 오류',request_id='existing')
        self.f.worker().run_once()
        self.assertEqual(self.tasks.show(job)['source'],'existing_queue')
        self.assertEqual(self.tasks.result(job)['runbook_context']['status'],'available')
        with self.assertRaises(ValueError):self.tasks.deliver(job)

    def test_unix_api_request_worker_result_and_local_mailbox(self):
        server=LocalTaskServer(self.tasks);thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.01});thread.start()
        try:
            receipt=request(self.f.store,{'action':'submit','request':self.raw})['value'];job=receipt['task_id']
            self.f.worker().run_once()
            result=request(self.f.store,{'action':'result','task_id':job})['value']
            self.assertEqual(result['runbook_context']['status'],'available')
            notice=request(self.f.store,{'action':'deliver','task_id':job})['value']
            self.assertEqual(notice['state'],'delivered')
        finally:server.shutdown();thread.join();server.server_close()
        self.assertFalse(Path(server.server_address).exists())

    def slack(self):
        policy=SlackPolicy.model_validate_json((ROOT/'config/slack-local.example.json').read_bytes())
        adapter=SlackAdapter(self.tasks,policy,SECRET);transport=FakeSlack(policy)
        event={'type':'event_callback','team_id':policy.team_id,'api_app_id':policy.app_id,'event_id':'EvRUNBOOK001',
            'event':{'type':'app_mention','user':'UALICE01','channel':'CLOCAL01','ts':'1000.000002','thread_ts':'1000.000001',
                     'text':f'<@{policy.bot_user_id}> investigate {self.f.target.alias} 캐시 오류'}}
        import hmac
        raw=canonical_json_bytes(event);stamp=str(int(self.f.time));signature='v0='+hmac.new(SECRET,b'v0:'+stamp.encode()+b':'+raw,hashlib.sha256).hexdigest()
        receipt=adapter.receive({'Content-Type':'application/json','X-Slack-Request-Timestamp':stamp,'X-Slack-Signature':signature},raw)
        queued=adapter.drain_one();self.f.worker().run_once()
        return adapter,transport,receipt,queued

    def test_signed_local_fixture_slack_reply_includes_bounded_source_citation(self):
        adapter,transport,receipt,queued=self.slack()
        reply=adapter.reply(receipt['request_id'],transport)
        self.assertEqual(reply['state'],'delivered');self.assertEqual(transport.calls,1)
        context=self.tasks.result(queued['task_id'])['runbook_context'];citation=context['citations'][0]
        self.assertIn(f"cache v1 {citation['section']['id']} SHA256 {citation['content_sha256']}",transport.last_payload['text'])
        self.assertNotIn(citation['excerpt']['text'],transport.last_payload['text'])
        self.assertFalse(reply['execution_allowed']);self.assertFalse(reply['human_read_confirmed'])

    def test_revoked_reference_never_leaks_in_fixture_reply(self):
        adapter,transport,receipt,queued=self.slack()
        self.policy_path.unlink();adapter.reply(receipt['request_id'],transport)
        self.assertIn('withheld',transport.last_payload['text'])
        self.assertNotIn('참고: cache',transport.last_payload['text'])

    def test_mid_reply_policy_revocation_is_checked_again_before_post(self):
        adapter,transport,receipt,queued=self.slack();original=self.tasks.result;calls=[]
        def changing(job):
            value=original(job);calls.append(1)
            if len(calls)==1:self.policy_path.unlink()
            return value
        with patch.object(self.tasks,'result',side_effect=changing):
            with self.assertRaises(ValueError):adapter._payload(self._slack_row(adapter,receipt))
        self.assertEqual(transport.calls,0)

    def test_revocation_after_reply_payload_before_post_sends_nothing(self):
        adapter,transport,receipt,queued=self.slack();original=adapter._payload
        def revoke(row):
            payload=original(row)
            # 완성된 전송 본문을 읽은 뒤 외부 정책 파일이 철회되는 경계를 재현한다.
            self.policy_path.unlink(missing_ok=True)
            return payload
        with patch.object(adapter,'_payload',side_effect=revoke):
            reply=adapter.reply(receipt['request_id'],transport)
        self.assertEqual(transport.calls,0)
        self.assertEqual(reply['state'],'needs_review')

    def test_revocation_during_reply_lookup_cannot_confirm_delivery(self):
        adapter,transport,receipt,queued=self.slack();transport.lost_response=True
        self.assertEqual(adapter.reply(receipt['request_id'],transport)['state'],'needs_review')
        original=transport.find_message
        def revoke(payload):
            found=original(payload);self.policy_path.unlink();return found
        with patch.object(transport,'find_message',side_effect=revoke):
            reply=adapter.reconcile_reply(receipt['request_id'],transport)
        self.assertEqual(reply['state'],'needs_review')
        self.assertEqual(transport.calls,1)

    def test_revocation_after_envelope_before_mailbox_publish_sends_nothing(self):
        job=self.complete();original=self.tasks._envelope
        def revoke(job):
            value=original(job);self.policy_path.unlink();return value
        with patch.object(self.tasks,'_envelope',side_effect=revoke):
            delivery=self.tasks.deliver(job)
        self.assertEqual(delivery['state'],'needs_review')
        self.assertFalse(list(self.tasks.mailbox.glob('notice-*.json')))
        self.assertEqual(self.f.queue.show(job)['attempts'],1)

    def test_revocation_during_mailbox_read_cannot_confirm_delivery(self):
        job=self.complete();self.tasks.deliver(job);original=self.tasks._read_delivery
        def revoke(identifier):
            value=original(identifier);self.policy_path.unlink();return value
        with patch.object(self.tasks,'_read_delivery',side_effect=revoke):
            delivery=self.tasks.reconcile_delivery(job)
        self.assertEqual(delivery['state'],'needs_review')
        self.assertEqual(delivery['attempts'],1)

    def _slack_row(self,adapter,receipt):
        with adapter._db() as db:return dict(db.execute('SELECT * FROM slack_requests WHERE request_id=?',(receipt['request_id'],)).fetchone())


if __name__=='__main__':unittest.main()
