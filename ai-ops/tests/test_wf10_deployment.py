"""Offline generated configuration contract, no cluster or Slack credentials."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from ai_ops.wf10_deployment import draft, candidate, build, verify, main, encoded
from ai_ops.wf10_preparation import LIMITS, UNRESOLVED
from ai_ops.slack_server import load_settings
from ai_ops.alert_receiver import load_receiver_settings
from ai_ops.b_contracts import load_b_settings
from ai_ops.runtime_settings import runbook_settings_kwargs
from ai_ops.event_store import EventStore
from ai_ops.job_queue import JobQueue

ROOT=Path(__file__).resolve().parents[2]

def fixture():
    config={name:json.loads((ROOT/'ai-ops/config'/src).read_text()) for name,src in [
        ('bindings.json','targets.practice.example.json'),('runtime.json','b-runtime.example.json'),
        ('metric-queries.json','metric-queries.json'),('runbook-policy.json','runbook-policy.local.example.json')]}
    b=config['bindings.json']['bindings'][0]
    b['alias']='url-shortener-b6-lab';b['target'].update(namespace='ai-ops-lab',workload='lab-api',service='lab-api')
    policy=config['runbook-policy.json']['targets'][0];policy.update(alias=b['alias'],target=deepcopy(b['target']))
    for conn in config['runtime.json']['connections'].values():
        prefix='/var/run/ai-ops/kubernetes' if conn['kind']=='kubernetes' else '/run/collector'
        conn.update(base_url='https://kubernetes.default.svc' if conn['kind']=='kubernetes' else 'https://prometheus.example.invalid:8443',
                    ca_file=prefix+'/ca.crt',token_file=prefix+'/token')
    return {**draft(),'namespace':'ai-ci','name':'wf10-fixture','pvc_name':'fixture-pvc','service_account':'fixture-readonly',
        'image':'example.invalid/ai-ops@sha256:'+'a'*64,'source_revision':'b'*40,
        'source':{'alias':'lab-seoul','source_id':'am-fixture','cluster_id':b['target']['cluster_id']},
        'slack':{'team_id':'TLOCAL01','app_id':'ALOCAL01','bot_user_id':'UBOT001','user_id':'UUSER01',
                 'investigation_channel_id':'CLOCAL01','alert_channel_id':'CALERT01'},
        'secrets':{'signing':'fixture-signing','bot':'fixture-bot','slack_tls':'fixture-tls',
                   'event_identity':'fixture-event','collector':'fixture-collector'},
        'network':{'dns':{'namespace':'kube-system','labels':{'k8s-app':'kube-dns'}},
            'alertmanager':{'namespace':'monitoring','labels':{'app':'alertmanager'}},
            'ingress':{'namespace':'traefik','labels':{'app':'traefik'}},
            'collector_cidrs':['10.0.0.1/32','10.0.0.2/32'],'slack_cidrs':['8.8.8.8/32']},'config':config}

class DeploymentTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.v=fixture()
        env=patch.dict(os.environ,{'AI_OPS_FILES_ROOT':str(self.root)});env.start();self.addCleanup(env.stop)
    def render(self):return candidate(self.v)
    def pod(self):return next(x for x in self.render()['resources'] if x['kind']=='Deployment')['spec']['template']['spec']
    def test_unresolved_draft_writes_no_manifest(self):
        out=self.root/'none';result=build(draft(),out)
        self.assertEqual(result['status'],'blocked_missing_inputs');self.assertFalse(out.exists())
        self.assertEqual(set(result['unresolved_live_inputs']),UNRESOLVED)
        self.assertFalse(result['execution_allowed'])
    def test_each_missing_input_blocks_render(self):
        for k in draft():
            if k in ('schema_version','kind','execution_allowed'):continue
            v=deepcopy(self.v);v[k]=None
            self.assertEqual(candidate(v)['resources'],[])
    def test_renderer_no_external_side_effects_and_deterministic(self):
        with patch('socket.socket.connect',side_effect=AssertionError('network')),patch('subprocess.Popen',side_effect=AssertionError('shell')):
            result=self.render()
        self.assertEqual(result,self.render());self.assertFalse(result['execution_allowed']);self.assertFalse(result['live_verified'])
        self.assertEqual(result['preserved_c_limits_seconds'],LIMITS)
    def test_manifest_has_no_rbac_secret_pvc_or_external_route_creation(self):
        self.assertEqual([x['kind'] for x in self.render()['resources']],['ConfigMap','Deployment','Service','Service','NetworkPolicy'])
        for service in self.render()['resources'][2:4]:self.assertEqual(service['spec']['type'],'ClusterIP')
    def test_one_pod_recreate_and_shared_existing_pvc(self):
        d=self.render()['resources'][1];pod=d['spec']['template']['spec']
        self.assertEqual(d['spec']['replicas'],1);self.assertEqual(d['spec']['strategy'],{'type':'Recreate'})
        self.assertEqual(len(pod['containers']),4)
        for c in pod['containers']:self.assertIn({'name':'data','mountPath':'/data','readOnly':False},c['volumeMounts'])
        self.assertEqual(next(x for x in pod['volumes'] if x['name']=='data')['persistentVolumeClaim']['claimName'],'fixture-pvc')
        # A recursive fsGroup pass on the shared PVC would widen the private queue store.
        self.assertEqual(pod['securityContext']['fsGroupChangePolicy'],'OnRootMismatch')
        self.assertEqual(pod['securityContext']['fsGroup'],10001)
    def test_least_credentials_per_role(self):
        p=self.pod();self.assertFalse(p['automountServiceAccountToken'])
        mounts={c['name']:{m['name'] for m in c['volumeMounts']} for c in p['containers']}
        self.assertNotIn('bot',mounts['slack-ingress']);self.assertNotIn('kubernetes',mounts['slack-relay'])
        self.assertIn('bot',mounts['slack-relay']);self.assertIn('kubernetes',mounts['worker'])
        self.assertNotIn('signing',mounts['event-receiver']);self.assertNotIn('event-identity',mounts['worker'])
    def test_every_process_has_liveness_and_init_has_bounded_resources(self):
        p=self.pod()
        for c in p['containers']:
            live=c['livenessProbe']
            self.assertEqual((live['periodSeconds'],live['timeoutSeconds'],live['failureThreshold']),(30,5,3))
            probe={k:v for k,v in c['readinessProbe'].items() if k in ('exec','tcpSocket')}
            self.assertEqual({k:v for k,v in live.items() if k in ('exec','tcpSocket')},probe)
        self.assertEqual(next(c for c in p['containers'] if c['name']=='worker')['livenessProbe']['exec']['command'],
                         ['python','-m','ai_ops.worker_runtime','--probe'])
        self.assertEqual(next(c for c in p['containers'] if c['name']=='event-receiver')['livenessProbe']['tcpSocket'],{'port':9443})
        for c in p['initContainers']:
            self.assertEqual(set(c['resources']),{'requests','limits'})
            self.assertEqual(c['resources']['limits']['memory'],'64Mi')
    def test_private_secret_copy_scripts_compile_without_user_commands(self):
        for c in self.pod()['initContainers']:
            self.assertEqual(c['command'][:2],['python','-c']);compile(c['command'][2],'<init>','exec')
            self.assertIn('chmod(0o600)',c['command'][2]);self.assertNotIn('fixture-',c['command'][2])
            self.assertTrue(c['securityContext']['readOnlyRootFilesystem'])
    def test_registry_and_runtime_command_consistent(self):
        out=self.render();cm=out['resources'][0]['data'];web=json.loads(cm['slack-web.json'])
        self.assertEqual(web['event_sources'],{'lab-seoul':{'source_id':'am-fixture','cluster_id':'practice-eks-example'}})
        c=next(x for x in self.pod()['containers'] if x['name']=='event-receiver')
        self.assertIn('--event-source-alias',c['args']);self.assertNotIn('/v1/alerts',encoded(out).decode())
    def test_only_specified_peers_and_https_egress(self):
        spec=self.render()['resources'][-1]['spec'];self.assertEqual(len(spec['ingress']),2)
        self.assertEqual(spec['egress'][-1]['ports'],[{'protocol':'TCP','port':443}])
        self.assertEqual(spec['egress'][1]['ports'],[{'protocol':'TCP','port':443},{'protocol':'TCP','port':8443}])
        self.assertNotIn('0.0.0.0/0',encoded(spec).decode())
    def test_wildcard_network_or_empty_selector_rejected(self):
        for key,value in [('collector_cidrs',['0.0.0.0/0']),('slack_cidrs',['10.0.0.1/32']),('slack_cidrs',['127.0.0.1/32'])]:
            v=deepcopy(self.v);v['network'][key]=value
            with self.assertRaises(ValueError):candidate(v)
        self.v['network']['ingress']['labels']={}
        with self.assertRaises(ValueError):self.render()
    def test_no_inline_secret_unknown_field_or_execution_flag(self):
        for key,value in [('token','synthetic-secret'),('execution_allowed',True),('execution_allowed',0),('schema_version',True)]:
            v=deepcopy(self.v);v[key]=value
            with self.assertRaises(ValueError):candidate(v)
    def test_target_scope_cluster_and_channel_change_rejected(self):
        for key,value in [('cluster_id','different')]:
            v=deepcopy(self.v);v['source'][key]=value
            with self.assertRaises(ValueError):candidate(v)
        self.v['slack']['alert_channel_id']='CLOCAL01'
        with self.assertRaises(ValueError):self.render()
    def test_recovery_permissions_mutable_tag_and_default_sa_rejected(self):
        for key,value in [('image','example.invalid/ai-ops:latest'),('service_account','default')]:
            v=deepcopy(self.v);v[key]=value
            with self.assertRaises(ValueError):candidate(v)
        self.v['secrets']['bot']=self.v['secrets']['signing']
        with self.assertRaises(ValueError):self.render()
    def test_settings_changes_do_not_weaken_limits_or_load_credentials(self):
        v=deepcopy(self.v);v['config']['runtime.json']['limits']['deadline_seconds']=59
        with self.assertRaises(ValueError):candidate(v)
        v=deepcopy(self.v);v['config']['runtime.json']['connections']['practice-kubernetes']['token_file']='/private/token'
        with self.assertRaises(ValueError):candidate(v)
        self.v['config']['runtime.json']['llm_enabled']=True
        with self.assertRaises(ValueError):self.render()
    def test_package_modes_no_overwrite_and_recomputed_hash_tamper(self):
        out=self.root/'out';build(self.v,out);self.assertFalse(verify(out)['execution_allowed'])
        self.assertEqual(out.stat().st_mode&0o777,0o700)
        for p in out.iterdir():self.assertEqual(p.stat().st_mode&0o777,0o600)
        with self.assertRaises(FileExistsError):build(self.v,out)
        p=out/'candidate.json';v=json.loads(p.read_text());v['resources'][1]['spec']['replicas']=2
        from ai_ops.wf10_preparation import digest
        v['manifest_sha256']=digest(encoded(v['resources']));p.write_bytes(encoded(v))
        with self.assertRaises(ValueError):verify(out)
    def test_symlink_extra_file_and_cli_apply_rejected(self):
        out=self.root/'out';build(self.v,out);alias=self.root/'alias';alias.symlink_to(out,target_is_directory=True)
        with self.assertRaises(ValueError):verify(alias)
        (out/'extra').touch()
        with self.assertRaises(ValueError):verify(out)
        with redirect_stdout(io.StringIO()),self.assertRaises(SystemExit):main(['apply'])
    def test_cli_rejection_does_not_echo_input(self):
        path=self.root/'input.json';path.write_text('{"token":"NEVER_ECHO_THIS"}')
        output=io.StringIO()
        with redirect_stdout(output):rc=main(['build','--input',str(path),'--output',str(self.root/'out')])
        self.assertEqual(rc,1);self.assertNotIn('NEVER_ECHO_THIS',output.getvalue())
    def test_receiver_worker_slack_hashes_match_and_event_can_be_requested(self):
        cm=self.render()['resources'][0]['data'];conf=self.root/'config';conf.mkdir()
        for name,data in cm.items():(conf/name).write_text(data)
        slack,policy=load_settings(conf);receiver=load_receiver_settings(conf)
        worker=load_b_settings(conf/'bindings.json',conf/'runtime.json',conf/'metric-queries.json',**runbook_settings_kwargs(conf))
        self.assertEqual(receiver.config_hash,slack.config_hash);self.assertEqual(worker.config_hash,slack.config_hash)
        # Before the receiver fix, the runbook hash was omitted and event inspection was unavailable.
        old=load_b_settings(conf/'bindings.json',conf/'runtime.json',conf/'metric-queries.json')
        self.assertNotEqual(old.config_hash,slack.config_hash)
        q=JobQueue(self.root/'store',clock=lambda:1000);sources={'lab-seoul':{'source_id':'am-fixture','cluster_id':'practice-eks-example'}}
        events=EventStore(q,receiver,sources=sources)
        with q.transaction() as db:
            self.assertEqual(db.execute('SELECT rules FROM event_policy').fetchone()[0],'[]')
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
        self.assertEqual(events.settings.config_hash,slack.config_hash)
        target=slack.bindings.bindings[0].target.model_dump()
        alert={'labels':{**target,'alertname':'CacheHitRateLow','ai_ops':'enabled'},'status':'firing',
            'fingerprint':'0123456789abcdef','startsAt':'1970-01-01T00:16:40Z'}
        old_store=EventStore(q,old,sources=sources);old_id=old_store.ingest('lab-seoul',alert)
        view=EventStore(q,slack,sources=sources)
        with self.assertRaisesRegex(ValueError,'changed event target'):
            view.request(old_id,request_id='old-req',question='status',subject='user',allowed_targets=['url-shortener-b6-lab'])
        alert['fingerprint']='1123456789abcdef';eid=events.ingest('lab-seoul',alert)
        key=view.request(eid,request_id='new-req',question='status',subject='user',allowed_targets=['url-shortener-b6-lab'])
        self.assertTrue(key)


    def test_duplicate_json_keys_rejected_by_cli(self):
        path=self.root/'duplicate.json';path.write_text('{"schema_version":1,"schema_version":1}')
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(['build','--input',str(path),'--output',str(self.root/'out')]),1)
        self.assertFalse((self.root/'out').exists())

    def test_manual_policy_and_live_approval_are_never_inferred_from_render(self):
        r=self.render()
        self.assertEqual(r['manual_policy']['rules'],[])
        self.assertIn('exact_connection_approval',r['required_live_gates'])
        self.assertIn('source_image_provenance',r['required_live_gates'])
        self.assertEqual(r['source_revision'],'b'*40)
        self.assertFalse(r['execution_allowed'])
