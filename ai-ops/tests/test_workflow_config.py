from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from ai_ops.workflow_config import FILES, load_local_workflow, main

ROOT=Path(__file__).resolve().parents[1]


class WorkflowConfigTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.path=self.root/'workflow.json'
        self.original=json.loads((ROOT/'config/request-workflow.local.example.json').read_text())
        self.path.write_text(json.dumps(self.original))
        for key in FILES:
            name=self.original[key];shutil.copyfile(ROOT/'config'/name,self.root/name)

    def change(self, key, value):
        self.path.write_text(json.dumps({**self.original,key:value}))

    def test_valid_same_settings_and_no_external_capability(self):
        config=load_local_workflow(self.path)
        self.assertFalse(config.summary()['execution_allowed'])
        self.assertFalse(config.summary()['external_connections_enabled'])
        self.assertIsNotNone(config.settings.runbooks)
        self.assertEqual(config.summary()['config_hash'],config.settings.config_hash)

    def test_live_mode_secrets_extra_fields_and_escaping_paths_rejected(self):
        for key,value in [('mode','live'),('token','DO_NOT_PRINT'),('schema_version',True),
                          ('bindings','../targets.json'),('bindings','/tmp/targets.json')]:
            with self.subTest(key=key,value=value):
                self.change(key,value)
                with self.assertRaises(ValueError):load_local_workflow(self.path)
        self.path.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(ValueError):load_local_workflow(self.path)

    def test_real_origin_and_credential_paths_rejected_before_any_connection(self):
        path=self.root/self.original['runtime'];original=json.loads(path.read_text())
        for key,value in [('base_url','https://example.invalid'),('token_file','/tmp/DO_NOT_READ'),('ca_file','/tmp/DO_NOT_READ')]:
            changed=json.loads(json.dumps(original));changed['connections']['practice-kubernetes'][key]=value
            path.write_text(json.dumps(changed))
            with self.subTest(key=key),self.assertRaises(ValueError):load_local_workflow(self.path)

    def test_unknown_slack_target_and_changed_runbook_policy_rejected(self):
        path=self.root/self.original['slack_policy'];original=path.read_text()
        policy=json.loads(original);policy['grants'][0]['targets']=['missing-target'];path.write_text(json.dumps(policy))
        with self.assertRaises(ValueError):load_local_workflow(self.path)
        path.write_text(original)
        path=self.root/self.original['runbook_policy'];policy=json.loads(path.read_text());policy['registry_sha256']='0'*64
        path.write_text(json.dumps(policy))
        with self.assertRaises(ValueError):load_local_workflow(self.path)

    def test_links_oversize_and_cli_error_do_not_leak_input(self):
        self.path.unlink();self.path.symlink_to(ROOT/'config/request-workflow.local.example.json')
        with self.assertRaises(ValueError):load_local_workflow(self.path)
        self.path.unlink();self.path.write_text('x'*65537)
        with self.assertRaises(ValueError):load_local_workflow(self.path)
        self.path.write_text('{"secret":"DO_NOT_PRINT"}')
        output=StringIO()
        with redirect_stdout(output):self.assertEqual(main(['--config',str(self.path)]),2)
        self.assertEqual(json.loads(output.getvalue()),{'status':'rejected'})
