"""Local-only wiring validation. Loading never starts a server, worker, or model."""
import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import stat

from ai_ops.b_contracts import BSettings, load_b_settings
from ai_ops.evidence import _json_object
from ai_ops.slack_adapter import SlackPolicy

FILES = ('bindings', 'runtime', 'metric_queries', 'runbook_policy', 'slack_policy')


def read_config(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError('workflow_config_regular_file_required')
    with path.open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('workflow_config_too_large')
    return _json_object(raw)


@dataclass(frozen=True)
class LocalWorkflowConfig:
    settings: BSettings
    slack: SlackPolicy

    def summary(self):
        return {'status':'valid', 'mode':'local_fixture',
                'config_hash':self.settings.config_hash,
                'target_count':len(self.settings.bindings.bindings),
                'slack_grant_count':len(self.slack.grants),
                'runbook_policy_hash':self.settings.runbooks.policy_hash,
                'external_connections_enabled':False, 'model_enabled':False,
                'execution_allowed':False}


def load_local_workflow(path):
    path = Path(path)
    value = read_config(path)
    if (not isinstance(value, dict) or set(value) != {'schema_version','mode',*FILES}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['mode'] != 'local_fixture'):
        raise ValueError('workflow_local_config_required')
    files = {}
    for key in FILES:
        name = value[key]
        if not isinstance(name,str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.json',name) is None:
            raise ValueError('workflow_config_reference_rejected')
        files[key] = path.parent/name
        read_config(files[key])  # Reject links/oversize/duplicate keys before shared loaders.
    settings = load_b_settings(files['bindings'],files['runtime'],files['metric_queries'],
                               runbook_policy=files['runbook_policy'])
    if (settings.runtime.llm_enabled
            or any(b.target.environment != 'practice' for b in settings.bindings.bindings)
            or any(any(v is not None for v in (c.base_url,c.ca_file,c.token_file))
                   for c in settings.runtime.connections.values())):
        raise ValueError('workflow_external_connection_not_enabled')
    slack = SlackPolicy.model_validate(read_config(files['slack_policy']))
    aliases = {b.alias for b in settings.bindings.bindings}
    if any(not set(g.targets) <= aliases for g in slack.grants):
        raise ValueError('workflow_slack_target_mismatch')
    return LocalWorkflowConfig(settings,slack)


def main(argv=None):
    parser=argparse.ArgumentParser(description='로컬 요청·조사·회신 연결 설정 검사 (실행/외부 연결 없음)')
    parser.add_argument('--config',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        result=load_local_workflow(args.config).summary()
    except (OSError,ValueError,TypeError,KeyError,RecursionError):
        print('{"status":"rejected"}')
        return 2
    print(json.dumps(result,ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
