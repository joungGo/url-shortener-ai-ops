"""Verify independent source ownership and block unreviewed live control cutover."""
from pathlib import Path
import json
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
LEGACY = 'f-lab-edu/F-Lab-DevOps'

def yaml_file(path):
    return json.loads(subprocess.check_output(['ruby','-rjson','-ryaml','-e',
        'puts JSON.generate(YAML.load_file(ARGV[0]))',str(path)],text=True))

def main():
    assert not (ROOT/'.github/workflows/ci-cd.yml').exists(), 'Production deployment must remain external'
    assert not (ROOT/'url-shortener').exists(), 'Application implementation must remain external'
    config=json.loads((ROOT/'integration/platform-source.json').read_text())
    workflow=yaml_file(ROOT/'.github/workflows/ai-ops-ci.yml')
    assert workflow['permissions']=={'contents':'read'}
    assert config['active_control_repository']==LEGACY
    assert config['live_control_cutover']=='pending_reinstallation_review'
    checkouts=[s['with'] for s in workflow['jobs']['contracts']['steps']
               if s.get('with',{}).get('repository')==config['repository']]
    assert len(checkouts)==1 and checkouts[0]['ref']==config['revision']
    assert re.fullmatch('[a-f0-9]{40}',config['revision'])
    assert checkouts[0]['persist-credentials'] is False
    assert set(checkouts[0]['sparse-checkout'].split())==set(config['paths'])
    for filename in ['ai-ops-lab-ci.yml','ai-ops-normal-ci.yml','ai-ops-c2-rollback.yml','ai-ops-c4-ready-rollback.yml']:
        jobs=yaml_file(ROOT/'.github/workflows'/filename)['jobs']
        assert all(j.get('if')==f"github.repository == '{LEGACY}' && github.ref == 'refs/heads/main'" for j in jobs.values()),filename
    image=yaml_file(ROOT/'.github/workflows/ai-ops-image.yml')['jobs']['image']
    assert image['if']=="${{ !inputs.publish || github.repository == '"+LEGACY+"' }}"
    for path in (ROOT/'.github/workflows').glob('*.yml'):
        for job in yaml_file(path)['jobs'].values():
            for step in job.get('steps',[]):
                if 'uses' in step:assert re.fullmatch(r'[A-Za-z0-9_./-]+@[a-f0-9]{40}',step['uses']),step['uses']
    fixture=ROOT/'ai-ops/tests/fixtures/platform/.github/workflows/ci-cd.yml'
    assert fixture.is_file()
    print('AI source boundary verified; platform contracts pinned; live cutover remains blocked.')

if __name__=='__main__':main()
