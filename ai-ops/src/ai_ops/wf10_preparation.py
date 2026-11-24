"""WF10 offline, disabled preparation bundle. No clients, credentials or live writers.

This is a review format, not a deployment/approval API. Even complete synthetic
checks cannot enable deployment. Hashes detect changes, not authorship/approval.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat

UNRESOLVED = frozenset((
    'alert_channel_id','current_bot_identity','source_alias','source_id','new_cluster_id',
    'image_digest','rendered_manifest_hash','rollback_bundle_hash','new_uid_binding',
    'normal_ci_run','actual_connection_approval','shared_event_slack_queue_storage_topology'))
BASELINES = {'ai':'1bea7d6f83e60b12ddb2fe04ce181cfecf5a17ff',
    'platform_last_verified':'bf6bcbc100cc4af47d7aba690db69d80a0a5298b',
    'notice_pin':'d6c2c8fca50feb8453948be284e7b9b7cccb5284'}
HISTORICAL = {'team_id':'T0AAZ9N5GT1','app_id':'A0CJHH8ULUC','investigation_channel':'C0CK82SD8NL',
    'requester':'U0ABBAD4FT7','target_alias':'url-shortener-b6-lab',
    'slack_events_url':'https://slack-ops.bidservice.store/slack/events'}
LIMITS = {'lease_request':3,'start':15,'minimum_review_after_notice':120,
    'review_deadline_from_start':210,'owner_rollback':240,'independent_rollback_start':300,'observation':120}

def encoded(value):
    return (json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n').encode()

def digest(data):return hashlib.sha256(data).hexdigest()

def inventory(value):
    expected={'schema_version':1,'kind':'wf10_review_inventory_not_runtime',
        'status':'plan_documented_execution_blocked','execution_approved':False,'deployable':False,
        'code_baseline':BASELINES,'historical_candidates':HISTORICAL,
        'required_unresolved':sorted(UNRESOLVED),'policy_proposal':{'default':'manual','automatic_rules':[], 'activate_now':False},
        'store_strategy':'new_empty_active_queue_archive_old_read_only','external_actions_performed':[]}
    # Compare canonical JSON too: bool/int coercion is not an approval check.
    try:
        candidate=dict(value)
        names=candidate['required_unresolved']
        if not isinstance(names,list) or any(not isinstance(x,str) for x in names):raise ValueError()
        if len(names)!=len(set(names)) or set(names)!=UNRESOLVED:raise ValueError()
        candidate['required_unresolved']=sorted(names)
        if encoded(candidate)!=encoded(expected):raise ValueError()
    except (ValueError,KeyError,TypeError):raise ValueError('wf10_inventory_rejected') from None
    return expected


def artifacts(value):
    inv=inventory(value)
    disabled={'enabled':False,'mode':'diagnostics','replicaCount':1,
        'image':{'repository':'','digest':''},'receiver':{'enabled':False},
        'observability':{'enabled':False},'retention':{'enabled':False}}
    web={'schema_version':1,'enabled':False,'bot_token_file':'/run/ai-ops-private/slack-bot-token',
        'signing_secret_file':'/run/ai-ops-private/slack-signing-secret','event_sources':{}}
    processes=[
        {'name':'event-receiver','module':'ai_ops.alert_receiver','credential_classes':['event-receiver-tls','alert-bearer'],
         'required_args':['--cluster-id','--event-source-alias','--event-source-id'],'endpoint':'/v1/events'},
        {'name':'worker','module':'ai_ops.worker','credential_classes':['existing-read-only-collector'],
         'contract':'reuse existing worker entrypoint and exclusive worker lock; no recovery write credentials'},
        {'name':'slack-ingress','module':'ai_ops.slack_server','action':'ingress','credential_classes':['slack-signing','slack-tls'],
         'enable_web':False},
        {'name':'slack-relay','module':'ai_ops.slack_server','action':'relay','credential_classes':['slack-signing','slack-bot'],
         'enable_web':False}]
    deployment={'kind':'wf10_process_review_not_kubernetes','deployable':False,'processes':processes,
        'resource_count':0,'source_registry':None,'image_digest':None,'namespace':None,
        'shared_store':{'namespace':None,'pvc_name':None,'store_path':'/data/incidents',
            'require_same_physical_store':True, 'databases':['queue/jobs.sqlite3','slack-web/inbox.sqlite3'], 'evidence_directories_required':True,'path_string_equality_sufficient':False,
            'sqlite_wal_requires_supported_local_filesystem':True,'cross_namespace_shared_pvc_assumed':False,
            'worker_instances':1,'replacement_strategy':'stop-drain-before-start'},
        'external_routes':[],'new_rbac_rules':[],'secret_values':False,'unresolved':sorted(UNRESOLVED),
        'preserved_c_limits_seconds':LIMITS,'activation_supported':False}
    transition={'kind':'wf10_transition_review','execution_allowed':False,'strategy':inv['store_strategy'],
        'steps':['freeze new admission and relay sends','drain/stop receiver, worker and relay',
            'after stopping all writers, backup queue/jobs.sqlite3 AND slack-web/inbox.sqlite3 plus evidence files; quick_check and checksum manifest',
            'archive old DB read-only; do not replay jobs, sending replies or approvals',
            'initialize empty active store with same reviewed source registry and settings',
            'verify event_meta=2, queue user_version=3, manual rules=[], no pre-request jobs',
            'confirm physical shared store, least credentials and identical process version/config',
            'obtain separate exact live package approval before any process or route activation'],
        'reuse_existing_db_exception':['verify source/config identity and backup on clone first',
            'preserve jobs and attempts; fence old authority and reconcile pending/sending',
            'old worker must reject new schema; never downgrade metadata in place'],
        'source_policy':'EventStore.set_policy expected_revision CAS, not direct SQL activation',
        'automatic_rules':[],'live_backup_verified':False,'live_schema_verified':False}
    rollback={'kind':'wf10_rollback_review','execution_allowed':False,
        'steps':['stop new intake and relay sends; preserve ambiguous reply states',
            'preserve post-cutover database/evidence before considering old snapshot',
            'reconcile all post-cutover requests and sends; do not silently overwrite them',
            'restore compatible reviewed code/config/database as a unit only after reconciliation',
            'preserve original direct Slack notice route; stop AI intake/worker if old auto route returns',
            'if safe restore cannot be proven, keep notices and hand off investigation manually'],
        'preserve':['original credentials','operator/login','shared PVC','DNS original values',
            'snapshots/KMS','state versions','old and new request/reply evidence'],
        'cleanup':'only resources matching predeclared owner/UID/hash; no live cleanup implementation',
        'blind_snapshot_restore':False,'automatic_replay':False}
    files={'inventory.json':encoded(inv),'helm.disabled.json':encoded(disabled),'slack-web.disabled.json':encoded(web),
           'deployment.review.json':encoded(deployment),'transition.review.json':encoded(transition),'rollback.review.json':encoded(rollback),
           'README.md':('WF10 OFFLINE REVIEW ONLY\n\nHelm disabled values render zero resources. Slack is disabled and has no event sources.\n'
                        'The review JSON files are not Kubernetes manifests or an installer. No apply/restore command is included.\n'
                        'Do not toggle an enable flag: prepare and approve a separate complete live package.\n'
                        'Checksums bind this draft only; they are not signatures or live readiness evidence.\n').encode()}
    files['CHECKSUMS.json']=encoded({name:digest(data) for name,data in sorted(files.items())})
    return files


def read_regular(path, limit=65536):
    if limit not in (65536, 131072):raise ValueError('wf10_file_limit_rejected')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size>limit:raise ValueError('wf10_file_rejected')
        data=os.read(fd,limit+1)
        if len(data)>limit:raise ValueError('wf10_file_rejected')
        return data
    finally:os.close(fd)


def verify_package(directory):
    root=Path(directory)
    if root.is_symlink() or not root.is_dir():raise ValueError('wf10_directory_rejected')
    expected=artifacts(json.loads(read_regular(root/'inventory.json')))
    if {p.name for p in root.iterdir()}!=set(expected):raise ValueError('wf10_file_set_changed')
    for name,content in expected.items():
        if read_regular(root/name)!=content:raise ValueError('wf10_artifact_changed')
    return {'status':'disabled_review_verified','files':len(expected),'deployable':False,
        'activation_supported':False,'live_inputs_verified':False,'execution_allowed':False}


def write_package(value,directory):
    files=artifacts(value);root=Path(directory)
    root.mkdir(mode=0o700,parents=False,exist_ok=False)
    # Exclusive creation; never overwrite a prior package or follow file links.
    for name,content in files.items():
        fd=os.open(root/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(content)
    return verify_package(root)


def transition_assessment(facts):
    """Trusted local rehearsal facts only, not an execution permission API."""
    required={'producers_stopped','backup_quick_check','backup_hash_verified','shared_database_confirmed',
        'same_code_config','manual_policy','empty_active_queue','schema_supported'}
    if set(facts)!=required or any(type(v) is not bool for v in facts.values()):raise ValueError('wf10_transition_facts_invalid')
    return {'state':'review_conditions_satisfied' if all(facts.values()) else 'blocked',
        'blockers':sorted(k for k,v in facts.items() if not v),'execution_allowed':False,'live_verified':False}


def rollback_assessment(facts):
    required={'producers_stopped','backup_verified','compatible_code_schema','post_cutover_archive_verified',
        'new_requests_reconciled','ambiguous_sends_reconciled','notice_route_preserved'}
    if set(facts)!=required or any(type(v) is not bool for v in facts.values()):raise ValueError('wf10_rollback_facts_invalid')
    return {'state':'restore_candidate_requires_live_approval' if all(facts.values()) else 'manual_handoff_no_restore',
        'blockers':sorted(k for k,v in facts.items() if not v),'execution_allowed':False,'live_verified':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description='WF10 disabled review bundle only; no deployment or credentials')
    sub=parser.add_subparsers(dest='action',required=True)
    build=sub.add_parser('build');build.add_argument('--inventory',type=Path,required=True);build.add_argument('--output',type=Path,required=True)
    check=sub.add_parser('verify');check.add_argument('--directory',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        result=write_package(json.loads(read_regular(args.inventory)),args.output) if args.action=='build' else verify_package(args.directory)
        print(json.dumps(result,sort_keys=True));return 0
    except (OSError,ValueError,TypeError,RecursionError):
        print('{"status":"rejected","execution_allowed":false}');return 1

if __name__=='__main__':raise SystemExit(main())
