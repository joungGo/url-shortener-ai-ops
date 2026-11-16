"""실제 복구 작성 블록을 합성 API로 실행해 기존 쓰기 전 조건을 대조한다."""

import ast
import base64
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_ops import c_trial_writer_api as writer
from ai_ops import c_trial_token_lifetime as lifetime
from ai_ops.c_recovery import _encoded_values
from test_c_trial_token_lifetime import NOW, response


class WriterPathTest(unittest.TestCase):
    def run_path(self, *, invalid_time=False, changed=None, cutoff=False):
        source=Path(__file__).resolve().parents[1]/'scripts/prepare_c4_recovery_pr.py'
        main=next(n for n in ast.parse(source.read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='main')
        main=next(n for n in ast.walk(main) if isinstance(n,ast.Try)
                  and any(isinstance(x,ast.ImportFrom) and x.module=='ai_ops.c_trial_notice' for x in n.body))
        start=next(i for i,n in enumerate(main.body) if isinstance(n,ast.ImportFrom)
                   and n.module=='ai_ops' and any(a.name=='c_trial_writer_api' for a in n.names))
        end=next(i for i,n in enumerate(main.body) if isinstance(n,ast.ImportFrom) and n.module=='ai_ops.c_trial_notice')
        code=compile(ast.fix_missing_locations(ast.Module(body=main.body[start:end],type_ignores=[])),str(source),'exec')
        self.calls=[];self.saved={};self.state={};base='a'*40;head='b'*40
        proposal=SimpleNamespace(proposal_id='synthetic-proposal',current_digest='sha256:'+'c'*64,
                                 desired_digest='sha256:'+'d'*64)
        prefix='/repos/'+writer.REPO
        def api(method,path,credential,payload=None):
            self.calls.append((method,path))
            if path=='/app':return {'id':writer.APP_ID,'slug':'c03-execution-lab-writer','permissions':writer.PERMISSIONS}
            if path==f'/app/installations/{writer.INSTALLATION_ID}':
                return {'app_id':writer.APP_ID,'repository_selection':'selected','permissions':writer.PERMISSIONS}
            if path.endswith('/access_tokens'):return response(seconds=3601 if invalid_time else 3600)
            if path=='/installation/token':return {}
            if path.startswith('/installation/repositories'):
                return {'total_count':1,'repositories':[{'id':writer.REPO_ID}]}
            if path==prefix+'/git/ref/heads/main':return {'object':{'sha':'f'*40 if changed=='main' else base}}
            if path==prefix+'/git/refs':return {}
            if path==prefix+'/contents/ai-ops/lab/values.yaml':return {'commit':{'sha':head}}
            if path.startswith(prefix+'/contents/'):
                return {'sha':'e'*40,'content':base64.b64encode(_encoded_values(proposal.current_digest).encode()).decode()}
            if path==prefix+'/commits/'+head:return {'author':{'id':writer.BOT_ID},'parents':[{'sha':base}]}
            if path.startswith(prefix+'/compare/'):
                return {'total_commits':1,'files':[{'filename':writer.FILE,'additions':2 if changed=='diff' else 1,'deletions':1}]}
            if path==prefix+'/pulls':return {'number':19,'html_url':'https://github.com/'+writer.REPO+'/pull/19'}
            if path==prefix+'/pulls/19':return {'user':{'id':writer.BOT_ID},'head':{'sha':head},'base':{'sha':base},'state':'open'}
            raise AssertionError('unexpected synthetic endpoint')
        def window():
            if cutoff:raise ValueError('manual_recovery_review_window_too_short')
        ns={'state':self.state,'save':lambda n,v:self.saved.update({n:deepcopy(v)}),
            'link':SimpleNamespace(pr_base_sha=base,link_hash='e'*64),'proposal':proposal,
            'plan':SimpleNamespace(trial_id=202611160099,minimum_review_seconds=120),
            'deadline':NOW+timedelta(seconds=210),'review_window':window,'base64':base64,
            '_encoded_values':_encoded_values}
        with patch.object(writer,'api',side_effect=api),patch.object(writer,'jwt',return_value='synthetic-jwt'), \
             patch.object(lifetime,'utcnow',return_value=NOW),patch.object(lifetime.time,'monotonic',return_value=100.):
            exec(code,ns)

    def test_actual_writer_block_accepts_valid_hour_and_reaches_synthetic_pr(self):
        self.run_path()
        self.assertEqual(self.state['stage'],'pr_created_waiting_notice')
        self.assertEqual(self.state['head_sha'],'b'*40)
        self.assertEqual(self.state['writer_token_time']['server_remaining_seconds'],3600)
        self.assertEqual(self.calls[-1],('DELETE','/installation/token'))

    def test_invalid_hour_stops_actual_writer_before_any_repository_write(self):
        with self.assertRaises(lifetime.TokenLifetimeRejected):self.run_path(invalid_time=True)
        self.assertEqual(self.state['writer_token_time']['reason'],'github_token_server_lifetime_rejected')
        self.assertFalse(any(method in ('PUT','POST') and '/repos/' in path for method,path in self.calls))
        self.assertEqual(self.calls[-1],('DELETE','/installation/token'))

    def test_changed_main_or_diff_still_stops_pr_and_revokes(self):
        for changed in ('main','diff'):
            with self.subTest(changed=changed),self.assertRaises(AssertionError):self.run_path(changed=changed)
            self.assertFalse(any(method=='POST' and path.endswith('/pulls') for method,path in self.calls))
            self.assertEqual(self.calls[-1],('DELETE','/installation/token'))

    def test_review_window_still_stops_before_branch_and_revokes(self):
        with self.assertRaisesRegex(ValueError,'^manual_recovery_review_window_too_short$'):self.run_path(cutoff=True)
        self.assertFalse(any(method=='POST' and path.endswith('/git/refs') for method,path in self.calls))
        self.assertEqual(self.calls[-1],('DELETE','/installation/token'))
