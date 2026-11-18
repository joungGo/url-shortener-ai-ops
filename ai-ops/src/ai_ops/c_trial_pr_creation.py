"""현재 복구안의 작성 App 경로. 독립 GET만 병렬화하고 쓰기 장벽은 유지한다."""

import base64
from concurrent.futures import ThreadPoolExecutor

from ai_ops import c_trial_writer_api as api
from ai_ops.c_recovery import _encoded_values
from ai_ops.c_recovery_read_timing import phase, submit


def create_recovery_pr(plan, proposal, link, *, review_window, report_token_time, record_created=lambda value: None):
    review_window()
    with phase('writer.jwt', read=True):
        assertion = api.jwt()
    with phase('writer.identity', read=True), ThreadPoolExecutor(max_workers=2) as pool:
        reg_read = submit(pool, api.api, 'GET', '/app', assertion)
        install_read = submit(pool, api.api, 'GET', '/app/installations/180499782', assertion)
        reg, install = reg_read.result(), install_read.result()
    api.require(reg['id'] == 5696038 and reg['slug'] == 'c03-execution-lab-writer'
                and reg['permissions'] == api.PERMISSIONS)
    api.require(install['app_id'] == 5696038 and install['repository_selection'] == 'selected'
                and install.get('suspended_at') is None and install['permissions'] == api.PERMISSIONS)
    # The token context owns revocation even when issuance returns after the deadline.
    with api.installation_token(assertion, report=report_token_time) as grant:
        prefix = '/repos/joungGo/c03-execution-lab'
        with phase('writer.scope_base', read=True), ThreadPoolExecutor(max_workers=3) as pool:
            repos_read = submit(pool, grant.api, 'GET', '/installation/repositories?per_page=100')
            main_read = submit(pool, grant.api, 'GET', prefix+'/git/ref/heads/main')
            before_read = submit(pool, grant.api, 'GET', prefix+'/contents/ai-ops/lab/values.yaml?ref='+link.pr_base_sha)
            repos, main, before = repos_read.result(), main_read.result(), before_read.result()
        api.require(repos['total_count'] == 1 and len(repos['repositories']) == 1
                    and repos['repositories'][0]['id'] == 1452572723)
        api.require(main['object']['sha'] == link.pr_base_sha)
        api.require(base64.b64decode(before['content'], validate=False) == _encoded_values(proposal.current_digest).encode())
        branch = 'codex/c04-recovery-'+str(plan.trial_id)+'-'+proposal.proposal_id[-8:]
        review_window()
        with phase('writer.branch', read=True):
            grant.api('POST', prefix+'/git/refs', {'ref':'refs/heads/'+branch, 'sha':link.pr_base_sha})
        review_window()
        with phase('writer.commit', read=True):
            created = grant.api('PUT', prefix+'/contents/ai-ops/lab/values.yaml', {
                'message':'C-04 격리 실습 이미지 복구\n\nRecovery-Proposal-SHA256: '+link.link_hash,
                'content':base64.b64encode(_encoded_values(proposal.desired_digest).encode()).decode(),
                'sha':before['sha'], 'branch':branch})
        head = created['commit']['sha']
        with phase('writer.commit_diff', read=True), ThreadPoolExecutor(max_workers=2) as pool:
            commit_read = submit(pool, grant.api, 'GET', prefix+'/commits/'+head)
            compare_read = submit(pool, grant.api, 'GET', prefix+'/compare/'+link.pr_base_sha+'...'+head)
            commit, comp = commit_read.result(), compare_read.result()
        api.require(commit['author']['id'] == 354870297 and [p['sha'] for p in commit['parents']] == [link.pr_base_sha])
        api.require(comp['total_commits'] == 1 and len(comp['files']) == 1
                    and comp['files'][0]['filename'] == 'ai-ops/lab/values.yaml'
                    and comp['files'][0]['additions'] == comp['files'][0]['deletions'] == 1)
        review_window()
        with phase('writer.pull', read=True):
            pr = grant.api('POST', prefix+'/pulls', {
                'title':'C-04 격리 실습 이미지 복구 '+str(plan.trial_id), 'head':branch, 'base':'main',
                'body':'격리 실습 lab-api의 이미지 digest 한 필드를 정상 이미지로 복구합니다.\n\nRecovery-Proposal-SHA256: '
                       +link.link_hash+'\n\nReview deadline '+plan.review_cutoff().isoformat()+'.'})
            # Persist a bounded reconciliation record even if this response is late.
            # This is not a verified PR, delivery receipt, or human approval.
            api.require(type(pr['number']) is int and pr['number'] > 0)
            api.require(pr['html_url'] == f"https://github.com/joungGo/c03-execution-lab/pull/{pr['number']}")
            record_created(dict(stage='created_unverified', trial_id=plan.trial_id,
                                pull_number=pr['number'], url=pr['html_url'], head_sha=head))
        with phase('writer.pull_verify', read=True):
            pull = grant.api('GET', prefix+'/pulls/'+str(pr['number']))
        api.require(pull['user']['id'] == 354870297 and pull['head']['sha'] == head
                    and pull['base']['sha'] == link.pr_base_sha and pull['state'] == 'open')
        ready = dict(stage='pr_created_waiting_notice', pull_number=pr['number'], url=pr['html_url'],
                     head_sha=head, review_deadline_utc=plan.review_cutoff().isoformat(),
                     minimum_review_seconds=plan.minimum_review_seconds)
    # Returning only after revocation lets the outer timing gate reserve actual delivery.
    return ready
