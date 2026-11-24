"""Offline WF10 change plan (A6): ordered apply + bound rollback bundle for ONE verified candidate.

Input is a candidate package produced and verified by ``ai_ops.wf10_deployment``.
Output is data only: no shell/kubectl strings, no client, no credentials, and
``execution_allowed`` is always false. The plan fixes *what* would change and how
it would be undone so that a later, separate live approval can bind to its hash.
It is not an approval, an installer, or evidence that any check passed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ai_ops.wf10_deployment import LIVE_GATES, PACKAGE_LIMIT, verify as verify_candidate
from ai_ops.wf10_preparation import LIMITS, digest, encoded, read_regular

# Policies before workloads (no unrestricted start window), config and Services
# before the Deployments that use them, sender before the main Pod that calls it.
APPLY_ORDER = (('ConfigMap', '-config'), ('NetworkPolicy', '-sender'), ('NetworkPolicy', ''),
               ('Service', '-sender'), ('Service', '-events'), ('Service', '-slack'),
               ('Deployment', '-sender'), ('Deployment', ''))
# With the optional recovery section: the egress proxy policy/Service/Pod come
# before the main Pod that needs them (same ordering rule as the sender).
APPLY_ORDER_RECOVERY = (('ConfigMap', '-config'), ('NetworkPolicy', '-sender'), ('NetworkPolicy', '-egress'),
                        ('NetworkPolicy', ''), ('Service', '-sender'), ('Service', '-egress'),
                        ('Service', '-events'), ('Service', '-slack'), ('Deployment', '-sender'),
                        ('Deployment', '-egress'), ('Deployment', ''))
# Pre-existing inputs the plan must never create, change or delete.
PROTECTED = ('Namespace', 'PersistentVolumeClaim', 'PersistentVolume', 'Secret', 'ServiceAccount',
             'Role', 'RoleBinding', 'ClusterRole', 'ClusterRoleBinding', 'Ingress', 'StorageClass')
PRE_CHECKS = ('candidate_package_verified', 'exact_change_plan_hash_approved_separately',
              'producers_stopped', 'backup_quick_check', 'backup_hash_verified', 'shared_database_confirmed',
              'same_code_config', 'manual_policy', 'empty_active_queue', 'schema_supported',
              'no_existing_resource_with_planned_names', 'server_side_dry_run_all_resources')
ROLLBACK_TRIGGERS = ('server_side_dry_run_or_apply_rejected', 'startup_window_exceeded_without_ready',
                     'egress_negative_test_failed', 'slack_identity_rejected',
                     'unexpected_resource_owner_or_uid', 'operator_abort')
POST_CHECKS = ('recorded_uid_for_every_applied_resource', 'both_deployments_ready_within_startup_window',
               'worker_egress_blocked_except_collectors_and_sender', 'sender_only_internet_egress',
               'slack_identity_verified_via_sender', 'manual_policy_rules_empty', 'no_jobs_before_first_request')
FILE = 'change-plan.json'


def _startup_window(resource):
    pod = resource['spec']['template']['spec']
    return max(c['startupProbe']['periodSeconds'] * c['startupProbe']['failureThreshold']
               for c in pod['containers'])


def plan(candidate_directory):
    verify_candidate(candidate_directory)  # re-renders inputs; rejects any change
    root = Path(candidate_directory)
    raw = read_regular(root / 'candidate.json', PACKAGE_LIMIT)
    candidate = json.loads(raw)
    resources = candidate['resources']
    main = next(r for r in resources if r['kind'] == 'Deployment'
                and not r['metadata']['name'].endswith(('-sender', '-egress')))
    name, namespace = main['metadata']['name'], main['metadata']['namespace']
    index = {(r['kind'], r['metadata']['name']): r for r in resources}
    egress = 'recovery' in candidate  # optional WF07 egress proxy resources
    expected = [(kind, name + suffix) for kind, suffix in (APPLY_ORDER_RECOVERY if egress else APPLY_ORDER)]
    if set(index) != set(expected) or len(index) != len(resources):
        raise ValueError('wf10_plan_resource_set_rejected')
    if any(r['metadata']['namespace'] != namespace or r['kind'] in PROTECTED for r in resources):
        raise ValueError('wf10_plan_scope_rejected')
    apply_steps = []
    for step, key in enumerate(expected, 1):
        resource = index[key]
        entry = {'step': step, 'action': 'apply_exact_manifest', 'kind': key[0], 'namespace': namespace,
                 'name': key[1], 'manifest_sha256': digest(encoded(resource)),
                 'requires': ['server_side_dry_run_passed', 'name_absent_before_apply'],
                 'record_after': ['uid', 'resourceVersion']}
        if key[0] == 'Deployment':
            entry['startup_window_seconds'] = _startup_window(resource)
        apply_steps.append(entry)
    rollback_steps = [{'step': i, 'action': 'delete_if_recorded_uid_matches', 'kind': s['kind'],
                       'namespace': namespace, 'name': s['name'], 'manifest_sha256': s['manifest_sha256']}
                      for i, s in enumerate(reversed(apply_steps), 1)]
    rollback = {
        'kind': 'wf10_rollback_bundle', 'execution_allowed': False,
        'triggers': list(ROLLBACK_TRIGGERS),
        'first': ['stop new intake and sender traffic', 'preserve queue/jobs.sqlite3 and slack-web/inbox.sqlite3 '
                  'with evidence before any delete (see transition/rollback review)'],
        'steps': rollback_steps,
        'match_rule': {'label': {'app.kubernetes.io/part-of': 'wf10'}, 'recorded_uid_required': True,
                       'unknown_or_changed_uid': 'stop_and_hand_off_manually'},
        'never_delete_or_modify': list(PROTECTED),
        'data_restore': 'not automatic; follow wf10 rollback review after reconciliation',
        'after': ['confirm only planned names removed', 'preserved PVC/Secrets unchanged',
                  'original Alertmanager direct notice route still delivering'],
    }
    rollback_hash = digest(encoded(rollback))
    document = {
        'schema_version': 1, 'kind': 'wf10_change_plan', 'status': 'plan_rendered_not_approved',
        'execution_allowed': False, 'live_verified': False,
        'candidate': {'candidate_json_sha256': digest(raw), 'manifest_sha256': candidate['manifest_sha256'],
                      'source_revision': candidate['source_revision'], 'namespace': namespace, 'name': name},
        'pre_checks': list(PRE_CHECKS), 'apply': apply_steps,
        'post_checks': list(POST_CHECKS) + (['egress_proxy_only_fixed_destinations',
                                             'main_pod_internet_egress_only_via_proxy'] if egress else []),
        'required_live_gates': list(LIVE_GATES), 'rollback': rollback, 'rollback_bundle_sha256': rollback_hash,
        'approval_binding': {'binds': ['change_plan_sha256', 'manifest_sha256', 'rollback_bundle_sha256'],
                             'granted': False, 'reuse_of_past_approvals': False},
        'preserved_c_limits_seconds': LIMITS,
    }
    return document


def build(candidate_directory, output):
    document = plan(candidate_directory)
    data = encoded(document)
    root = Path(output)
    root.mkdir(mode=0o700, exist_ok=False)
    fd = os.open(root / FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
    return {'status': document['status'], 'change_plan_sha256': digest(data),
            'manifest_sha256': document['candidate']['manifest_sha256'],
            'rollback_bundle_sha256': document['rollback_bundle_sha256'], 'execution_allowed': False}


def verify(candidate_directory, directory):
    root = Path(directory)
    if root.is_symlink() or {p.name for p in root.iterdir()} != {FILE}:
        raise ValueError('wf10_plan_package_rejected')
    data = read_regular(root / FILE)
    if encoded(plan(candidate_directory)) != data:
        raise ValueError('wf10_plan_changed')
    return {'status': 'plan_verified_not_approved', 'change_plan_sha256': digest(data),
            'execution_allowed': False, 'live_verified': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline WF10 change plan; never applies or deletes')
    sub = parser.add_subparsers(dest='action', required=True)
    b = sub.add_parser('build')
    b.add_argument('--candidate', type=Path, required=True)
    b.add_argument('--output', type=Path, required=True)
    v = sub.add_parser('verify')
    v.add_argument('--candidate', type=Path, required=True)
    v.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = (build(args.candidate, args.output) if args.action == 'build'
                  else verify(args.candidate, args.directory))
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, TypeError, KeyError, OSError, RecursionError, StopIteration):
        print('{"status":"rejected","execution_allowed":false}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
