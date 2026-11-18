"""합성 API 지연 비교. 실제 GitHub·AWS·Slack에는 연결하지 않는다."""
from collections import Counter
from concurrent.futures import Future
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
import test_c_trial_writer_path as fixtures
from ai_ops import c_trial_pr_creation as creation


def main():
    def serial(pool, function, *args):
        future=Future()
        try:future.set_result(function(*args))
        except BaseException as exc:future.set_exception(exc)
        return future
    original=creation.submit
    report={'scope':'synthetic local API only; not live latency or C04 completion',
            'injected_api_delay_ms':25,'writer':{}}
    counts={}
    for name,submit in [('serial',serial),('parallel',original)]:
        fixture=fixtures.WriterPathTest()
        start=time.perf_counter()
        with patch.object(creation,'submit',submit):
            fixture.run_path(hook=lambda method,path:time.sleep(.025))
        elapsed=(time.perf_counter()-start)*1000
        counts[name]=Counter(fixture.calls)
        assert fixture.state['stage']=='pr_created_waiting_notice'
        assert fixture.calls[-1]==('DELETE','/installation/token')
        report['writer'][name]={'duration_ms':round(elapsed,3),'api_calls':len(fixture.calls)}
    assert counts['serial']==counts['parallel']
    report['same_api_calls_and_gates']=True
    report['recorded_006']={'proposal_seconds':43.423622,'writer_seconds':7.415729,
        'remaining_at_proposal_start_seconds':49.808440,'old_notice_deficit_seconds':1.030911}
    report['sub_budgets']={'writer_minimum_seconds':8,'notice_minimum_seconds':4,
        'proposal_available_in_006_seconds':37.808440}
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
