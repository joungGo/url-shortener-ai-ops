"""Offline bundle and synthetic SQLite cutover rehearsal; no live source input."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from ai_ops.wf10_preparation import (artifacts,write_package,verify_package,transition_assessment,
    rollback_assessment,read_regular,main)
from ai_ops.slack_runtime import SlackWebConfig,SlackRuntime
from ai_ops.event_store import EventStore
from ai_ops.job_queue import JobQueue
import test_slack_events as fixtures

ROOT=Path(__file__).resolve().parents[2]
INVENTORY=ROOT/'ai-ops/docs/wf10-connection-inputs-20261119.json'

def transition_facts():
    return dict.fromkeys(['producers_stopped','backup_quick_check','backup_hash_verified','shared_database_confirmed',
        'same_code_config','manual_policy','empty_active_queue','schema_supported'],True)

def rollback_facts():
    return dict.fromkeys(['producers_stopped','backup_verified','compatible_code_schema','post_cutover_archive_verified',
        'new_requests_reconciled','ambiguous_sends_reconciled','notice_route_preserved'],True)

class PackageTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.out=self.root/'package';self.inv=json.loads(INVENTORY.read_text())
    def build(self):return write_package(self.inv,self.out)
    def test_build_without_network_shell_or_credentials(self):
        with patch('socket.socket.connect',side_effect=AssertionError('network')),patch('subprocess.Popen',side_effect=AssertionError('shell')):
            result=self.build()
        self.assertFalse(result['deployable']);self.assertFalse(result['execution_allowed']);self.assertEqual(result['files'],8)
        self.assertEqual(self.out.stat().st_mode&0o777,0o700)
        for f in self.out.iterdir():self.assertEqual(f.stat().st_mode&0o777,0o600)
    def test_build_is_deterministic(self):self.assertEqual(artifacts(self.inv),artifacts(deepcopy(self.inv)))
    def test_inventory_activation_scope_changes_rejected(self):
        for key,value in [('deployable',True),('execution_approved',True),('execution_approved',0),('schema_version',True),
                          ('required_unresolved',[]),('external_actions_performed',['slack'])]:
            changed=deepcopy(self.inv);changed[key]=value
            with self.assertRaises(ValueError):artifacts(changed)
    def test_unknown_secret_fields_not_copied(self):
        changed=deepcopy(self.inv);changed['bot_token']='synthetic-secret'
        with self.assertRaises(ValueError):write_package(changed,self.out)
        self.assertFalse(self.out.exists())
    def test_automatic_rules_and_historical_target_changes_rejected(self):
        for path,value in [('policy_proposal',{'default':'automatic','automatic_rules':['*'],'activate_now':False}),
                           ('historical_candidates',{})]:
            changed=deepcopy(self.inv);changed[path]=value
            with self.assertRaises(ValueError):artifacts(changed)
    def test_duplicate_unresolved_rejected(self):
        self.inv['required_unresolved'].append(self.inv['required_unresolved'][0])
        with self.assertRaises(ValueError):self.build()
    def test_tampered_enable_rejected_even_with_new_checksum(self):
        self.build();p=self.out/'helm.disabled.json';data=json.loads(p.read_text());data['enabled']=True;p.write_text(json.dumps(data))
        m=self.out/'CHECKSUMS.json';checks=json.loads(m.read_text());checks[p.name]=hashlib.sha256(p.read_bytes()).hexdigest();m.write_text(json.dumps(checks))
        with self.assertRaises(ValueError):verify_package(self.out)
    def test_extra_and_missing_files_rejected(self):
        self.build();extra=self.out/'apply.sh';extra.write_text('not executable')
        with self.assertRaises(ValueError):verify_package(self.out)
        extra.unlink();(self.out/'rollback.review.json').unlink()
        with self.assertRaises(ValueError):verify_package(self.out)
    def test_symlink_file_and_directory_rejected(self):
        self.build();alias=self.root/'alias';alias.symlink_to(self.out,target_is_directory=True)
        with self.assertRaises(ValueError):verify_package(alias)
        p=self.out/'README.md';p.unlink();p.symlink_to(INVENTORY)
        with self.assertRaises(OSError):verify_package(self.out)
    def test_existing_output_not_overwritten(self):
        self.build();before=(self.out/'CHECKSUMS.json').read_bytes()
        with self.assertRaises(FileExistsError):self.build()
        self.assertEqual(before,(self.out/'CHECKSUMS.json').read_bytes())
    def test_oversize_input_rejected(self):
        p=self.root/'large';p.write_bytes(b'x'*65537)
        with self.assertRaises(ValueError):read_regular(p)
    def test_slack_disabled_config_rejected_before_secret_read(self):
        self.build();config=SlackWebConfig.load(self.out/'slack-web.disabled.json')
        self.assertFalse(config.enabled);self.assertEqual(config.event_sources,{})
        with patch('ai_ops.slack_runtime.read_private_secret',side_effect=AssertionError('secret')):
            with self.assertRaisesRegex(ValueError,'slack_web_disabled'):SlackRuntime(None,None,config)
    def test_helm_disabled_renders_no_resources(self):
        self.build();helm=shutil.which('helm');self.assertIsNotNone(helm,'Helm required for disabled render contract')
        run=subprocess.run([helm,'template','wf10-offline',str(ROOT/'ai-ops/chart'),'-f',str(self.out/'helm.disabled.json')],capture_output=True,text=True,check=True,timeout=30)
        self.assertEqual(run.stdout.strip(),'')
    def test_process_review_has_no_apply_resource_and_no_write_credentials(self):
        d=json.loads(artifacts(self.inv)['deployment.review.json'])
        self.assertNotIn('apiVersion',d);self.assertEqual(d['resource_count'],0);self.assertFalse(d['activation_supported'])
        ingress=next(p for p in d['processes'] if p['name']=='slack-ingress')
        self.assertEqual(ingress['credential_classes'],['slack-signing','slack-tls'])
        self.assertIsNone(d['shared_store']['pvc_name']);self.assertFalse(d['shared_store']['path_string_equality_sufficient'])
    def test_transition_gates_each_missing_condition_and_never_authorizes(self):
        facts=transition_facts();self.assertEqual(transition_assessment(facts)['state'],'review_conditions_satisfied')
        for key in facts:
            result=transition_assessment(facts|{key:False});self.assertEqual(result['blockers'],[key]);self.assertFalse(result['execution_allowed'])
        with self.assertRaises(ValueError):transition_assessment(facts|{'backup_quick_check':1})
    def test_rollback_gates_new_requests_sends_schema_and_notice(self):
        facts=rollback_facts()
        for key in facts:
            result=rollback_assessment(facts|{key:False});self.assertEqual(result['state'],'manual_handoff_no_restore');self.assertFalse(result['execution_allowed'])
        self.assertFalse(rollback_assessment(facts)['execution_allowed'])
    def test_cli_does_not_offer_live_actions(self):
        with self.assertRaises(SystemExit):main(['apply'])

class CutoverRehearsalTest(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.SlackEventsTest();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.root=self.f.fixture.root/'wf10';self.root.mkdir(mode=0o700)
    def backup(self,name):
        dest=self.root/name
        with sqlite3.connect(self.f.queue.path.as_uri()+'?mode=ro',uri=True) as src:
            with sqlite3.connect(dest) as dst:src.backup(dst);self.assertEqual(dst.execute('PRAGMA quick_check').fetchone()[0],'ok')
        dest.chmod(0o600)
        with sqlite3.connect(self.f.adapter.path.as_uri()+'?mode=ro',uri=True) as src:
            with sqlite3.connect(str(dest)+'.slack') as dst:
                src.backup(dst);self.assertEqual(dst.execute('PRAGMA quick_check').fetchone()[0],'ok')
        Path(str(dest)+'.slack').chmod(0o600)
        return dest
    def test_backup_keeps_pending_jobs_new_store_does_not_replay(self):
        self.f.run_request();archive=self.backup('archive.sqlite3');saved=hashlib.sha256(archive.read_bytes()).hexdigest()
        fresh=JobQueue(self.root/'fresh',clock=lambda:self.f.fixture.time)
        store=EventStore(fresh,self.f.fixture.settings,sources=self.f.sources)
        with fresh.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT rules FROM event_policy').fetchone()[0],'[]')
            self.assertEqual(db.execute('SELECT version FROM event_meta').fetchone()[0],2)
        with sqlite3.connect(archive.as_uri()+'?mode=ro',uri=True) as db:self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],1)
        self.assertEqual(saved,hashlib.sha256(archive.read_bytes()).hexdigest())
    def test_shared_store_identity_and_registry_mismatch(self):
        second=JobQueue(self.f.queue.root,clock=lambda:self.f.fixture.time)
        store=EventStore(second,self.f.fixture.settings,sources=self.f.sources)
        self.assertEqual(store.resolve_reference(self.f.ref),self.f.eid)
        changed=deepcopy(self.f.sources);changed['lab-seoul']['source_id']='other'
        with self.assertRaisesRegex(ValueError,'registry changed'):EventStore(second,self.f.fixture.settings,sources=changed)
    def test_clone_schema_transition_preserves_original_and_jobs(self):
        self.f.run_request();before=self.backup('before.sqlite3');original=before.read_bytes()
        clone=JobQueue(self.root/'clone',clock=lambda:self.f.fixture.time)
        with sqlite3.connect(before.as_uri()+'?mode=ro',uri=True) as src:
            with sqlite3.connect(clone.path) as dst:src.backup(dst);dst.execute('UPDATE event_meta SET version=1')
        EventStore(clone,self.f.fixture.settings,sources=self.f.sources)
        with clone.transaction() as db:
            self.assertEqual(db.execute('SELECT version FROM event_meta').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],1)
        self.assertEqual(original,before.read_bytes())
    def test_post_cutover_request_and_uncertain_send_block_blind_restore(self):
        self.f.run_request();self.backup('before.sqlite3')
        self.f.run_request(self.f.command());after=self.backup('after.sqlite3');saved=after.read_bytes()
        for field in ['new_requests_reconciled','ambiguous_sends_reconciled']:
            result=rollback_assessment(rollback_facts()|{field:False})
            self.assertEqual(result['state'],'manual_handoff_no_restore')
        with sqlite3.connect(Path(str(after)+'.slack').as_uri()+'?mode=ro',uri=True) as db:self.assertEqual(db.execute('SELECT count(*) FROM slack_requests').fetchone()[0],2)
        self.assertEqual(saved,after.read_bytes())
