"""Run synthetic delayed reads + local Git/Helm only; never contact live services."""
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_c_proposal_latency import ProposalLatencyTest


def main():
    output={'scope':'synthetic local reads; not live latency or C04 success'}
    for method,key,attribute in [
        ('test_local_delayed_reads_are_faster_without_removing_checks','audit','latency_report'),
        ('test_link_reuses_only_sha_objects_with_new_reader_and_rechecks_main_twice','link','link_metrics')]:
        test=ProposalLatencyTest(method);test.setUp()
        try:
            getattr(test,method)();output[key]=getattr(test,attribute)
        finally:test.doCleanups()
    print(json.dumps(output,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
