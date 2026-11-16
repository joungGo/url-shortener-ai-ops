"""등록·해시 검증된 런북의 로컬 키워드/메타데이터 검색. 실행 권한은 부여하지 않는다."""

import argparse
from datetime import date
import hashlib
import json
import os
import re
import stat
import unicodedata

from pydantic import Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

from ai_ops import replay_tools as tools
from ai_ops.contracts import StrictModel, canonical_json_bytes
from ai_ops.evidence import _json_object, redact_text


LIMIT=65536


class CodeSource(StrictModel):
    path: StrictStr = Field(pattern=r'^[A-Za-z0-9_./-]{1,256}$')
    content_sha256: StrictStr = Field(pattern=r'^[a-f0-9]{64}$')

    @field_validator('path')
    @classmethod
    def relative(cls,value):
        if value.startswith('/') or '..' in value.split('/'):raise ValueError('invalid_source_reference')
        return value


class RegistryEntry(StrictModel):
    document_id: StrictStr = Field(pattern=r'^[a-z0-9][a-z0-9._-]{0,63}$')
    document_version: StrictInt = Field(ge=1,le=1000000)
    path: StrictStr
    content_sha256: StrictStr = Field(pattern=r'^[a-f0-9]{64}$')
    verified_against_git_sha: StrictStr = Field(pattern=r'^[a-f0-9]{40}$')
    checked_at: StrictStr = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    verification_scope: StrictStr = Field(min_length=1,max_length=100)
    live_verified: StrictBool
    sources: list[CodeSource] = Field(min_length=1,max_length=32)

    @model_validator(mode='after')
    def checked(self):
        if self.path!=f'ai-ops/runbooks/{self.document_id}.md':raise ValueError('invalid_document_path')
        date.fromisoformat(self.checked_at)
        if len({s.path for s in self.sources})!=len(self.sources):raise ValueError('duplicate_source_reference')
        return self


class SearchMetadata(StrictModel):
    document_id: StrictStr = Field(pattern=r'^[a-z0-9][a-z0-9._-]{0,63}$')
    document_version: StrictInt = Field(ge=1,le=1000000)
    content_sha256: StrictStr = Field(pattern=r'^[a-f0-9]{64}$')
    services: list[StrictStr] = Field(min_length=1,max_length=16)
    symptoms: list[StrictStr] = Field(min_length=1,max_length=24)
    failure_types: list[StrictStr] = Field(min_length=1,max_length=24)
    keywords: list[StrictStr] = Field(min_length=1,max_length=32)

    @field_validator('services','symptoms','failure_types','keywords')
    @classmethod
    def labels(cls,values):
        if any(not 1<=len(v)<=64 or any(unicodedata.category(c).startswith('C') for c in v) for v in values):
            raise ValueError('invalid_search_label')
        if any(redact_text(v)[1] for v in values):raise ValueError('secret_in_search_metadata')
        if len(set(_normalize(v) for v in values))!=len(values):raise ValueError('duplicate_search_label')
        return values


class MetadataIndex(StrictModel):
    schema_version: StrictInt = Field(ge=1,le=1)
    registry_sha256: StrictStr = Field(pattern=r'^[a-f0-9]{64}$')
    documents: list[SearchMetadata] = Field(min_length=1,max_length=32)


class SearchRequest(StrictModel):
    query: StrictStr = Field(default='',max_length=512)
    document_ids: list[StrictStr] | None = Field(default=None,max_length=32)
    service: StrictStr | None = Field(default=None,max_length=64)
    symptom: StrictStr | None = Field(default=None,max_length=64)
    failure_type: StrictStr | None = Field(default=None,max_length=64)
    code_revision: StrictStr | None = Field(default=None,pattern=r'^[a-f0-9]{40}$')
    document_version: StrictInt | None = Field(default=None,ge=1,le=1000000)
    limit: StrictInt = Field(default=5,ge=1,le=10)


def _normalize(value):
    return unicodedata.normalize('NFKC',value).casefold()


def _read(root,name):
    if root.is_symlink() or not root.is_dir():raise tools.ToolDenied('runbook_root_unavailable')
    fd=os.open(root/name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>LIMIT:
            raise tools.ToolDenied('runbook_search_file_rejected')
        with os.fdopen(fd,'rb',closefd=False) as stream:raw=stream.read(LIMIT+1)
        if len(raw)>LIMIT:raise tools.ToolDenied('runbook_search_file_too_large')
        return raw
    finally:os.close(fd)


def _registry(raw):
    """기존 index.yaml의 제한된 알려진 형식만 읽는다. YAML 실행 기능은 사용하지 않는다."""
    entries=[];current=None;source=None;headers=[]
    for line in raw.decode('utf-8').splitlines():
        if not line.strip() or line.startswith('#'):continue
        match=re.fullmatch(r'  - document_id: ("[^"\n]+")',line)
        if match:
            current={'document_id':json.loads(match.group(1)),'sources':[]};entries.append(current);source=None
        elif current is not None and (match:=re.fullmatch(r'    ([a-z_][a-z0-9_]*): (.+)',line)):
            key=match.group(1)
            if key in current:raise tools.ToolDenied('duplicate_registry_field')
            current[key]=json.loads(match.group(2))
        elif line=='    sources:' and current is not None:
            if source is not None:raise tools.ToolDenied('duplicate_registry_sources')
            source={}
        elif source is not None and (match:=re.fullmatch(r'      - path: ("[^"\n]+")',line)):
            source={'path':json.loads(match.group(1))};current['sources'].append(source)
        elif source is not None and (match:=re.fullmatch(r'        content_sha256: ("[a-f0-9]+")',line)):
            if 'content_sha256' in source:raise tools.ToolDenied('duplicate_source_hash')
            source['content_sha256']=json.loads(match.group(1))
        elif current is None and line in ('index_format_version: 1','documents:'):
            headers.append(line)
        else:raise tools.ToolDenied('unsupported_registry_format')
    if headers!=['index_format_version: 1','documents:'] or not 1<=len(entries)<=32:
        raise tools.ToolDenied('invalid_runbook_registry')
    parsed=[RegistryEntry.model_validate(v) for v in entries]
    if len({e.document_id for e in parsed})!=len(parsed):raise tools.ToolDenied('duplicate_document_id')
    return {e.document_id:e for e in parsed}


def _sections(text):
    lines=text.splitlines(keepends=True);sections=[];path=[];start=0;heading='문서 머리';level=0;fence=None
    for index,line in enumerate(lines):
        candidate=re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$',line.rstrip('\r\n'))
        if candidate:
            marker=candidate.group(1)
            if fence is None:fence=(marker[0],len(marker))
            elif marker[0]==fence[0] and len(marker)>=fence[1] and not candidate.group(2).strip():fence=None
            continue
        if fence is not None:continue
        match=re.match(r'^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*$',line.rstrip('\r\n'))
        if match:
            if index>start:sections.append({'start':start,'end':index,'heading':heading,'level':level,'path':list(path)})
            level=len(match.group(1));heading=re.sub(r'[ \t]+#+$','',match.group(2))
            path=[h for h in path if h[0]<level]+[(level,heading)]
            start=index
    if len(lines)>start:sections.append({'start':start,'end':len(lines),'heading':heading,'level':level,'path':list(path)})
    if not sections or len(sections)>128:raise tools.ToolDenied('runbook_section_limit')
    return sections


def _matches(term,value):
    normalized=_normalize(value)
    if any('\uac00'<=c<='\ud7a3' for c in term):return term in normalized
    words=set(re.findall(r'\w+',normalized))
    # 영문 오류명 뒤에 붙은 한국어 조사도 검색한다 (ImagePullBackOff만으로).
    words.update(re.findall(r'[a-z0-9_]+',normalized))
    words.update(part for word in tuple(words) for part in word.split('_'))
    return term in words


def _display(value):
    value,_=redact_text(value)
    return ''.join(' ' if unicodedata.category(c).startswith('C') else c for c in value)


class RunbookSearch:
    def __init__(self,*,allowed_document_ids=None):
        # 기존 조회 도구와 같은 신뢰 설정이다. 질의에서 경로/새 root를 받지 않는다.
        self.root=tools.RUNBOOK_ROOT
        self.allowed=None if allowed_document_ids is None else frozenset(allowed_document_ids)

    def search(self,query='',**filters):
        try:return self._search(SearchRequest.model_validate({'query':query,**filters}))
        except tools.ToolDenied:raise
        except (ValueError,TypeError,AttributeError,KeyError,OSError,RecursionError):
            raise tools.ToolDenied('runbook_search_rejected') from None

    def _search(self,request):
        if any(unicodedata.category(c).startswith('C') for c in request.query):raise tools.ToolDenied('invalid_search_query')
        cleaned,_=redact_text(request.query)
        terms=list(dict.fromkeys(re.findall(r'\w+',_normalize(cleaned))))
        if len(terms)>12 or any(not 2<=len(t)<=64 for t in terms) or (cleaned.strip() and not terms):
            raise tools.ToolDenied('invalid_search_query')
        index_raw=_read(self.root,'index.yaml');index_hash=hashlib.sha256(index_raw).hexdigest();registry=_registry(index_raw)
        metadata_raw=_read(self.root,'search-metadata.json');metadata=MetadataIndex.model_validate(_json_object(metadata_raw))
        if metadata.registry_sha256!=index_hash:raise tools.ToolDenied('runbook_registry_metadata_stale')
        mapped={m.document_id:m for m in metadata.documents}
        if len(mapped)!=len(metadata.documents) or set(mapped)!=set(registry):raise tools.ToolDenied('runbook_metadata_coverage_rejected')
        for identifier,meta in mapped.items():
            entry=registry[identifier]
            if meta.document_version!=entry.document_version or meta.content_sha256!=entry.content_sha256:
                raise tools.ToolDenied('runbook_metadata_version_or_hash_stale')
        allowed=set(registry) if self.allowed is None else set(self.allowed)
        selected=allowed if request.document_ids is None else set(request.document_ids)
        if not allowed<=set(registry) or not selected<=allowed:raise tools.ToolDenied('runbook_search_scope_rejected')
        hits=[];searched=0
        for identifier in sorted(selected):
            entry=registry[identifier];meta=mapped[identifier]
            if any(value is not None and _normalize(value) not in {_normalize(v) for v in getattr(meta,key)}
                   for key,value in [('services',request.service),('symptoms',request.symptom),('failure_types',request.failure_type)]):continue
            if request.code_revision is not None and request.code_revision!=entry.verified_against_git_sha:continue
            if request.document_version is not None and request.document_version!=entry.document_version:continue
            raw=_read(self.root,identifier+'.md')
            if hashlib.sha256(raw).hexdigest()!=entry.content_sha256:raise tools.ToolDenied('runbook_hash_mismatch')
            original=raw.decode('utf-8');safe,_=redact_text(original)
            # 기존 get_runbook의 등록/버전/해시/마스킹 판정을 재사용한다.
            legacy=tools._runbook(identifier)
            if (legacy['version']!=str(entry.document_version) or legacy['content_sha256']!=entry.content_sha256
                    or legacy['content']!=safe):raise tools.ToolDenied('runbook_changed_during_search')
            lines=safe.splitlines();raw_lines=original.splitlines(keepends=True)
            if len(lines)!=len(raw_lines):raise tools.ToolDenied('runbook_redaction_line_mapping_rejected')
            sections=_sections(original);searched+=1
            metadata_matches={t:[field for field in ('services','symptoms','failure_types','keywords')
                if any(_matches(t,v) for v in getattr(meta,field))] for t in terms}
            doc_hits=[]
            for section in sections:
                positions={t:[i+1 for i in range(section['start'],section['end']) if _matches(t,lines[i])] for t in terms}
                if any(not positions[t] and not metadata_matches[t] for t in terms):continue
                actual=[t for t in terms if positions[t]]
                if terms and not actual:continue
                safe_heading=_display(section['heading'])
                score=sum(4+3*int(_matches(t,safe_heading)) for t in actual)+sum(bool(v) for v in metadata_matches.values())
                doc_hits.append(self._hit(entry,meta,section,lines,raw_lines,score,terms,positions,metadata_matches,
                    index_hash,hashlib.sha256(metadata_raw).hexdigest(),'keyword' if terms else 'metadata_filter'))
            if not terms:
                doc_hits=doc_hits[:1]
            if terms and not doc_hits and all(metadata_matches[t] for t in terms):
                doc_hits=[self._hit(entry,meta,sections[0],lines,raw_lines,len(terms),terms,{t:[] for t in terms},
                    metadata_matches,index_hash,hashlib.sha256(metadata_raw).hexdigest(),'metadata_keyword')]
            hits.extend(doc_hits)
        if _read(self.root,'index.yaml')!=index_raw or _read(self.root,'search-metadata.json')!=metadata_raw:
            raise tools.ToolDenied('runbook_catalog_changed_during_search')
        hits.sort(key=lambda h:(-h['score'],h['document_id'],h['section']['start_line']))
        result={'status':'available' if hits else 'unavailable','hits':hits[:request.limit],
                'total_matches':len(hits),'truncated':len(hits)>request.limit,'searched_documents':searched,
                'execution_allowed':False,'current_runtime_verified':False,'external_model_transfer':False}
        if len(canonical_json_bytes(result))>LIMIT:raise tools.ToolDenied('runbook_search_output_limit')
        return result

    @staticmethod
    def _hit(entry,meta,section,lines,raw_lines,score,terms,positions,metadata_matches,index_hash,metadata_hash,kind):
        matching=[line for values in positions.values() for line in values]
        first=min(matching)-1 if matching else section['start']
        first=max(section['start'],first-1);last=min(section['end'],first+5)
        excerpt='\n'.join(_display(v) for v in lines[first:last]);shown=excerpt[:400]
        return {'document_id':entry.document_id,'document_version':entry.document_version,'content_sha256':entry.content_sha256,
            'path':entry.path,'registry_sha256':index_hash,'metadata_sha256':metadata_hash,
            'section':{'id':f"L{section['start']+1}-L{section['end']}",'heading':_display(section['heading']),
                'heading_path':[_display(title) for _,title in section['path']],
                'start_line':section['start']+1,'end_line':section['end'],
                'content_sha256':hashlib.sha256(''.join(raw_lines[section['start']:section['end']]).encode()).hexdigest()},
            'excerpt':{'text':shown,'start_line':first+1,'end_line':first+max(1,len(shown.splitlines())),
                'truncated':len(excerpt)>400,'redacted':True},
            'match_kind':kind,'score':score,'matches':[{'term':t,'lines':positions[t][:3],
                'metadata_fields':metadata_matches[t]} for t in terms],
            'metadata':meta.model_dump(exclude={'document_id','document_version','content_sha256'}),
            'verification':{'checked_at':entry.checked_at,'recorded_code_revision':entry.verified_against_git_sha,
                'recorded_scope':_display(entry.verification_scope),'recorded_live_verified':entry.live_verified,
                'current_source_code_checked':False,'current_runtime_verified':False},
            'content_role':'untrusted_reference_data','execution_allowed':False}


class SearchArgumentParser(argparse.ArgumentParser):
    def error(self,message):
        # argparse 기본 오류에는 유효하지 않은 원문 인자가 포함될 수 있다.
        raise tools.ToolDenied('invalid_search_arguments')


def main(argv=None):
    parser=SearchArgumentParser(description='등록된 런북의 로컬 출처 포함 키워드·메타데이터 검색')
    parser.add_argument('--query',default='');parser.add_argument('--document-id',action='append',dest='document_ids')
    parser.add_argument('--service');parser.add_argument('--symptom');parser.add_argument('--failure-type')
    parser.add_argument('--code-revision');parser.add_argument('--document-version',type=int);parser.add_argument('--limit',type=int,default=5)
    try:
        args=vars(parser.parse_args(argv))
        result=RunbookSearch().search(**args);print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except tools.ToolDenied:
        print('{"status":"rejected","execution_allowed":false}');return 2


if __name__=='__main__':raise SystemExit(main())
