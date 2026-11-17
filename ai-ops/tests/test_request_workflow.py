import importlib.util
from pathlib import Path
import unittest

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/check_request_workflow_local.py'
spec=importlib.util.spec_from_file_location('workflow_acceptance',SCRIPT)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class RequestWorkflowTest(unittest.TestCase):
    def test_same_request_signed_http_to_provenance_reply_and_restart(self):
        result=module.verify(SCRIPT.parents[1]/'config/request-workflow.local.example.json')
        self.assertEqual(result['status'],'passed')
        self.assertTrue(result['servers_threads_socket_and_fixture_store_removed'])
        self.assertFalse(result['execution_allowed'])

    def test_cli_rejects_invalid_config_without_echoing_sensitive_values(self):
        import json
        import os
        import shutil
        import subprocess
        import sys
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            source=SCRIPT.parents[1]/'config'
            manifest=json.loads((source/'request-workflow.local.example.json').read_text())
            for key in ('bindings','runtime','metric_queries','runbook_policy','slack_policy'):
                shutil.copyfile(source/manifest[key],root/manifest[key])
            config=root/'workflow.json';config.write_text(json.dumps(manifest))
            slack=root/manifest['slack_policy'];body=json.loads(slack.read_text())
            canary='LOCAL-CONFIG-SECRET-MUST-NOT-LEAK'
            body['bot_token']=canary;slack.write_text(json.dumps(body))
            result=subprocess.run([sys.executable,str(SCRIPT),'--config',str(config)],
                capture_output=True,text=True,timeout=10,
                env={**os.environ,'PYTHONPATH':str(SCRIPT.parents[1]/'src')})
            self.assertNotIn(canary,result.stdout+result.stderr)
            self.assertEqual(result.returncode,2)
            self.assertEqual(json.loads(result.stdout),{'status':'failed','code':'local_workflow_verification_failed'})
            self.assertEqual(result.stderr,'')
