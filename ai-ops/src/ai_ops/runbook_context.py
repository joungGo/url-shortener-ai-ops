"""대상에 고정한 런북 참고 자료를 조사·결과에 결합한다. 관측/실행 권한은 부여하지 않는다."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal
import hashlib

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from ai_ops.contracts import StrictModel, Target, Sha256, IncidentId, RunId, canonical_json_bytes, sha256_json
from ai_ops.evidence import _json_object
from ai_ops import replay_tools
from ai_ops.runbook_search import RunbookSearch, _matches, _normalize, _read, _registry

DocumentId = Annotated[StrictStr, Field(pattern=r'^[a-z0-9][a-z0-9._-]{0,63}$')]


class Grant(StrictModel):
    alias: StrictStr = Field(pattern=r'^[a-z][a-z0-9-]{0,62}$')
    target: Target
    document_ids: list[DocumentId] = Field(max_length=10)

    @model_validator(mode='after')
    def unique(self):
        if len(set(self.document_ids)) != len(self.document_ids):
            raise ValueError('duplicate_runbook_grant')
        return self


class Policy(StrictModel):
    schema_version: StrictInt = Field(ge=1, le=1)
    registry_sha256: Sha256
    metadata_sha256: Sha256
    targets: list[Grant] = Field(max_length=100)

    @model_validator(mode='after')
    def unique(self):
        if (len({g.alias for g in self.targets}) != len(self.targets)
                or len({sha256_json(g.target.model_dump()) for g in self.targets}) != len(self.targets)):
            raise ValueError('duplicate_runbook_target')
        return self


class Section(StrictModel):
    id: StrictStr = Field(pattern=r'^L[1-9][0-9]*-L[1-9][0-9]*$')
    heading: StrictStr = Field(max_length=512)
    heading_path: list[Annotated[StrictStr, Field(max_length=512)]] = Field(max_length=6)
    start_line: StrictInt = Field(ge=1, le=65536)
    end_line: StrictInt = Field(ge=1, le=65536)
    content_sha256: Sha256


class Excerpt(StrictModel):
    text: StrictStr = Field(max_length=400)
    start_line: StrictInt = Field(ge=1, le=65536)
    end_line: StrictInt = Field(ge=1, le=65536)
    truncated: StrictBool
    redacted: Literal[True]


class Verification(StrictModel):
    checked_at: StrictStr = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    recorded_code_revision: StrictStr = Field(pattern=r'^[0-9a-f]{40}$')
    recorded_scope: StrictStr = Field(max_length=100)
    recorded_live_verified: StrictBool
    current_source_code_checked: Literal[False]
    current_runtime_verified: Literal[False]


class Citation(StrictModel):
    citation_id: StrictStr = Field(pattern=r'^rb-[0-9a-f]{64}$')
    document_id: DocumentId
    document_version: StrictInt = Field(ge=1, le=1000000)
    content_sha256: Sha256
    path: StrictStr
    registry_sha256: Sha256
    metadata_sha256: Sha256
    section: Section
    excerpt: Excerpt
    match_kind: Literal['keyword', 'metadata_keyword']
    search_term: StrictStr = Field(min_length=2, max_length=64)
    verification: Verification
    content_role: Literal['untrusted_reference_data']
    execution_allowed: Literal[False]

    @model_validator(mode='after')
    def checked(self):
        if self.path != f'ai-ops/runbooks/{self.document_id}.md':
            raise ValueError('runbook_citation_path_rejected')
        s, e = self.section, self.excerpt
        if (s.id != f'L{s.start_line}-L{s.end_line}'
                or not s.start_line <= e.start_line <= e.end_line <= s.end_line
                or self.citation_id != citation_id(self.model_dump(mode='json'))):
            raise ValueError('runbook_citation_position_rejected')
        return self


def citation_id(value):
    return 'rb-' + sha256_json({key: value[key] for key in (
        'document_id', 'document_version', 'content_sha256', 'registry_sha256', 'metadata_sha256', 'section')})


class Context(StrictModel):
    schema_version: StrictInt = Field(ge=1, le=1)
    policy_hash: Sha256
    config_hash: Sha256
    incident_id: IncidentId
    run_id: RunId
    bundle_hash: Sha256
    report_hash: Sha256
    question_hash: Sha256
    target: Target
    status: Literal['available', 'unavailable', 'not_allowed', 'rejected']
    citations: list[Citation] = Field(max_length=3)
    usage: Literal['reference_only_not_observed_evidence']
    execution_allowed: Literal[False]
    current_runtime_verified: Literal[False]
    external_model_transfer: Literal[False]

    @model_validator(mode='after')
    def checked(self):
        if (bool(self.citations) != (self.status == 'available')
                or len({c.citation_id for c in self.citations}) != len(self.citations)):
            raise ValueError('runbook_context_status_rejected')
        return self


def read_saved(directory):
    raw = _read(directory, 'runbook-context.json')
    if len(raw) > 16000:
        raise ValueError('runbook_context_size_rejected')
    return _json_object(raw)


def validate_saved(value, request, bundle, report):
    context = Context.model_validate(value)
    if (context.incident_id != request.incident_id or context.run_id != report.run_id
            or context.bundle_hash != bundle.bundle_hash or context.target != bundle.target
            or context.report_hash != sha256_json(report.model_dump(mode='json'))
            or context.question_hash != sha256_json(request.question)
            or len(canonical_json_bytes(value)) > 16000):
        raise ValueError('runbook_context_binding_rejected')
    return context.model_dump(mode='json')


class RunbookPolicy:
    """기동 시 읽는 신뢰 설정. 요청은 이 경로/허용 목록을 지정할 수 없다."""
    def __init__(self, path: Path, registry):
        self.path = path
        self.root = replay_tools.RUNBOOK_ROOT
        raw = _read(path.parent, path.name)
        policy = Policy.model_validate(_json_object(raw))
        self.file_hash = hashlib.sha256(raw).hexdigest()
        self.policy_hash = sha256_json(policy.model_dump(mode='json'))
        self._active(registry)

    def _active(self, registry):
        raw = _read(self.path.parent, self.path.name)
        policy = Policy.model_validate(_json_object(raw))
        if (hashlib.sha256(raw).hexdigest() != self.file_hash
                or sha256_json(policy.model_dump(mode='json')) != self.policy_hash
                or replay_tools.RUNBOOK_ROOT != self.root
                or hashlib.sha256(_read(self.root, 'index.yaml')).hexdigest() != policy.registry_sha256
                or hashlib.sha256(_read(self.root, 'search-metadata.json')).hexdigest() != policy.metadata_sha256):
            raise ValueError('runbook_policy_changed')
        registered = _registry(_read(self.root, 'index.yaml'))
        for grant in policy.targets:
            if not set(grant.document_ids) <= set(registered):
                raise ValueError('unregistered_runbook_grant')
            binding = next((b for b in registry.bindings if b.alias == grant.alias), None)
            if binding is None or binding.target != grant.target:
                raise ValueError('runbook_policy_target_rejected')
        return policy

    def build(self, request, bundle, report, settings):
        base = {'schema_version': 1, 'policy_hash': self.policy_hash, 'config_hash': settings.config_hash,
                'incident_id': request.incident_id, 'run_id': report.run_id, 'bundle_hash': bundle.bundle_hash,
                'report_hash': sha256_json(report.model_dump(mode='json')), 'question_hash': sha256_json(request.question),
                'target': bundle.target.model_dump(mode='json'), 'status': 'rejected', 'citations': [],
                'usage': 'reference_only_not_observed_evidence', 'execution_allowed': False,
                'current_runtime_verified': False, 'external_model_transfer': False}
        try:
            policy = self._active(settings.bindings)
            grant = next((g for g in policy.targets if g.target == bundle.target), None)
            if grant is None or not grant.document_ids:
                base['status'] = 'not_allowed'
            else:
                search = RunbookSearch(allowed_document_ids=grant.document_ids)
                catalog = search.search(limit=10)
                # 허용된 문서의 등록 키워드만 선택한다. 요청에서 문서/서비스/경로를 받지 않는다.
                question = _normalize(request.question)
                facts = _normalize(' '.join(f.statement for f in report.facts[:10]))
                citations = []
                for hit in catalog['hits']:
                    candidates = sorted(hit['metadata']['keywords'], key=lambda v: (-len(v), _normalize(v)))
                    term = next((t for text in (question, facts) for t in candidates if _matches(_normalize(t), text)), None)
                    if term is None: continue
                    result = search.search(term, document_ids=[hit['document_id']], limit=1)
                    if not result['hits']: continue
                    found = result['hits'][0]
                    value = {k: found[k] for k in ('document_id', 'document_version', 'content_sha256', 'path',
                        'registry_sha256', 'metadata_sha256', 'section', 'excerpt', 'match_kind', 'verification',
                        'content_role', 'execution_allowed')}
                    value['search_term'] = term
                    value['citation_id'] = citation_id(value)
                    citations.append(Citation.model_validate(value).model_dump(mode='json'))
                    if len(citations) == 3: break
                base.update(status='available' if citations else 'unavailable', citations=citations)
            self._active(settings.bindings)
            return validate_saved(base, request, bundle, report)
        except (ValueError, TypeError, KeyError, OSError, RecursionError):
            # 참고 검색 실패를 관측·진단 성공이나 실행 허가로 바꾸지 않는다.
            base.update(status='rejected', citations=[])
            return validate_saved(base, request, bundle, report)

    def expose(self, saved, request, bundle, report, settings):
        try:
            value = validate_saved(saved, request, bundle, report)
            self._active(settings.bindings)
            if (value['policy_hash'] != self.policy_hash or value['config_hash'] != settings.config_hash
                    or value != self.build(request, bundle, report, settings)):
                raise ValueError('runbook_reference_changed')
            return value
        except (ValueError, TypeError, KeyError, OSError, RecursionError):
            return withheld()


def withheld():
    return {'status': 'withheld', 'citations': [], 'usage': 'reference_only_not_observed_evidence',
            'execution_allowed': False, 'current_runtime_verified': False, 'external_model_transfer': False}


def render_context(value):
    from ai_ops.reports import _safe_markdown
    context = Context.model_validate(value)
    lines = ['', '## 런북 참고 자료 (관측 증거·승인·실행 결과 아님)', '',
             f'- 조회 상태: {context.status}', f'- 정책 SHA256: `{context.policy_hash}`']
    for citation in context.citations:
        lines += ['', f'- 인용: `{citation.citation_id}` · `{citation.document_id}` v{citation.document_version}',
                  f'  - 원문: `{citation.path}` · SHA256 `{citation.content_sha256}`',
                  f'  - 절: {_safe_markdown(citation.section.heading)} · {citation.section.id} · SHA256 `{citation.section.content_sha256}`',
                  f'  - 일치: {citation.match_kind} · 확인일 {citation.verification.checked_at} · 현재 소스/실환경 미검증',
                  f'  - 발췌: {_safe_markdown(citation.excerpt.text)}']
    return '\n'.join(lines) + '\n'
