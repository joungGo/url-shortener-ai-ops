"""원문과 검색 결과의 출처를 독립 재계산한다. 외부 서비스에 연결하지 않는다."""
import hashlib
import json
from unittest.mock import patch

from ai_ops import replay_tools
from ai_ops.runbook_search import RunbookSearch


def main():
    checked = 0
    samples = []
    with patch('socket.create_connection', side_effect=AssertionError('network_forbidden')), \
         patch('subprocess.Popen', side_effect=AssertionError('command_forbidden')):
        search = RunbookSearch()
        for query, identifier in [('캐시 오류', 'cache'), ('DB 지연', 'database'),
                                  ('ImagePullBackOff', 'deployment')]:
            result = search.search(query)
            hit = result['hits'][0]
            assert hit['document_id'] == identifier
            assert not result['execution_allowed'] and not result['external_model_transfer']
            for hit in result['hits']:
                raw = (replay_tools.RUNBOOK_ROOT / (hit['document_id'] + '.md')).read_bytes()
                assert hashlib.sha256(raw).hexdigest() == hit['content_sha256']
                section = hit['section']
                selected = raw.decode().splitlines(keepends=True)[section['start_line'] - 1:section['end_line']]
                assert hashlib.sha256(''.join(selected).encode()).hexdigest() == section['content_sha256']
                assert not hit['verification']['current_source_code_checked']
                assert not hit['verification']['current_runtime_verified']
                checked += 1
            samples.append({'query': query, 'top_document_id': identifier,
                            'top_section': result['hits'][0]['section']['id']})
        assert [h['document_id'] for h in search.search(service='redis', symptom='캐시 오류')['hits']] == ['cache']
        assert not search.search('登録されていない症状')['hits']
        assert RunbookSearch(allowed_document_ids=['cache']).search()['total_matches'] == 1
    print(json.dumps({'status': 'passed', 'samples': samples, 'provenance_recomputed_hits': checked,
                      'metadata_filter': True, 'scope_enforced': True, 'external_network_calls': 0,
                      'slack_calls': 0, 'model_calls': 0, 'fault_injections': 0,
                      'recovery_executions': 0, 'execution_allowed': False}, ensure_ascii=False))


if __name__ == '__main__': main()
