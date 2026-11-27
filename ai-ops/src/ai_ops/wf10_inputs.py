"""WF10 candidate inputs for a practice install: read-only cluster facts + operator choices.

WF11 follow-up (2026-11-27). Replaces the personal reinstall-kit script with a tested tool.

* ``collect`` runs exactly five read-only ``kubectl get`` commands and writes a facts file
  (lab collector config, API server endpoints, the two Service IPs, the Prometheus Pod IP).
  No secrets are read.
* ``build`` is a pure function: facts + operator choices -> WF10 inputs, validated by the
  renderer (``wf10_deployment.inputs`` and ``candidate``) before an exclusive write.

Rules fixed here because a live install got them wrong once (2026-11-26):
``collector_cidrs`` always contains the API server endpoints, **both Service ClusterIPs**
(VPC CNI network policy matches the Service IP before DNAT) and the Prometheus Pod IP.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from ai_ops.path_policy import PACKAGE_ROOT

LAB_NAMESPACE, LAB_CONFIGMAP = 'ai-ops-lab', 'lab-collector'
PROMETHEUS_POD, PROMETHEUS_SERVICE = 'prometheus-kube-prometheus-stack-prometheus-0', 'kube-prometheus-stack-prometheus'
READS = {
    'lab_config': ['-n', LAB_NAMESPACE, 'get', 'configmap', LAB_CONFIGMAP, '-o', 'json'],
    'endpoints': ['-n', 'default', 'get', 'endpointslice', '-l', 'kubernetes.io/service-name=kubernetes', '-o', 'json'],
    'kubernetes_service': ['-n', 'default', 'get', 'service', 'kubernetes', '-o', 'json'],
    'prometheus_service': ['-n', 'monitoring', 'get', 'service', PROMETHEUS_SERVICE, '-o', 'json'],
    'prometheus_pod': ['-n', 'monitoring', 'get', 'pod', PROMETHEUS_POD, '-o', 'json'],
}
LAB_KEYS = ('bindings.json', 'runtime.json', 'metric-queries.json')
SLACK_ID = re.compile(r'[A-Z][A-Z0-9]{2,31}')
REGISTRY = '764428911194.dkr.ecr.ap-northeast-2.amazonaws.com/urlshortener-practice-ai-ops'
IMAGE = re.compile(re.escape(REGISTRY) + r'@sha256:[0-9a-f]{64}')
SHA = re.compile(r'[0-9a-f]{40}')

# Defaults confirmed in the 2026-11-26 install (operator choices; override per install if changed).
DEFAULTS = {
    'team_id': 'T0AAZ9N5GT1', 'app_id': 'A0CJHH8ULUC', 'user_id': 'U0ABBAD4FT7',
    'investigation_channel_id': 'C0CK82SD8NL', 'approver_github_id': 149856960,
    'author_github_id': 354870297,  # c03-execution-lab-writer[bot]; must differ from the approver
    'aws_role_name': 'urlshortener-practice-recovery-ecr-read',
}
SLACK_OVERRIDES = ('team_id', 'app_id', 'user_id', 'investigation_channel_id')  # CLI-overridable; GitHub IDs stay fixed
SECRETS = {'signing': 'ai-ops-slack-signing', 'bot': 'ai-ops-slack-bot', 'slack_tls': 'ai-ops-slack-tls',
           'event_identity': 'ai-ops-events-identity', 'sender_client': 'ai-ops-slack-sender-client'}
RECOVERY_SECRETS = {'reader': 'ai-ops-recovery-reader', 'github': 'ai-ops-recovery-github'}
CI_RELAY_SECRET, PUBLISH_SECRET = 'ai-ops-ci-github', 'ai-ops-pr-writer'


def _ip(value):
    address = ipaddress.ip_address(str(value))
    if address.version != 4 or address.is_loopback or address.is_link_local or address.is_unspecified:
        raise ValueError('wf10_inputs_address_rejected')
    return str(address)


def kubectl_reader(kubeconfig=None):
    def read(name):
        if name not in READS:
            raise ValueError('wf10_inputs_read_rejected')
        command = ['kubectl', '--request-timeout=25s'] + (['--kubeconfig', str(kubeconfig)] if kubeconfig else []) + READS[name]
        result = subprocess.run(command, capture_output=True, timeout=45)
        if result.returncode:
            raise ValueError('wf10_inputs_read_failed')
        return json.loads(result.stdout)
    return read


def collect(read):
    """Facts from five fixed reads (``read(name) -> parsed JSON``). No secret objects are read."""
    data = read('lab_config')['data']
    lab = {key: json.loads(data[key]) for key in LAB_KEYS}
    endpoints = sorted({_ip(address) for item in read('endpoints')['items']
                        for endpoint in item.get('endpoints') or [] for address in endpoint['addresses']})
    if not endpoints:
        raise ValueError('wf10_inputs_endpoints_missing')
    return {'schema_version': 1, 'kind': 'wf10_input_facts', 'lab_config': lab, 'api_endpoints': endpoints,
            'kubernetes_service_ip': _ip(read('kubernetes_service')['spec']['clusterIP']),
            'prometheus_service_ip': _ip(read('prometheus_service')['spec']['clusterIP']),
            'prometheus_pod_ip': _ip(read('prometheus_pod')['status']['podIP'])}


def collector_cidrs(facts):
    """API endpoints + BOTH Service IPs + the Prometheus Pod IP, as unique /32s."""
    ips = set(facts['api_endpoints']) | {facts['kubernetes_service_ip'], facts['prometheus_service_ip'],
                                         facts['prometheus_pod_ip']}
    return sorted(_ip(ip) + '/32' for ip in ips)


def build(facts, *, bot_user_id, alert_channel_id, image, source_revision, normal_binding,
          ci_relay=True, publish=True, overrides=None):
    """Pure assembly + renderer validation. Raises on anything the renderer would reject."""
    from ai_ops.wf10_deployment import candidate, draft, inputs
    if facts.get('kind') != 'wf10_input_facts' or facts.get('schema_version') != 1:
        raise ValueError('wf10_inputs_facts_rejected')
    if not set(overrides or {}) <= set(DEFAULTS):
        raise ValueError('wf10_inputs_override_rejected')
    o = {**DEFAULTS, **(overrides or {})}
    for value in (bot_user_id, alert_channel_id, o['team_id'], o['app_id'], o['user_id'], o['investigation_channel_id']):
        if not isinstance(value, str) or not SLACK_ID.fullmatch(value):
            raise ValueError('wf10_inputs_slack_id_rejected')
    if alert_channel_id == o['investigation_channel_id'] or bot_user_id == o['user_id']:
        raise ValueError('wf10_inputs_slack_boundary_rejected')
    if o['author_github_id'] == o['approver_github_id']:
        raise ValueError('wf10_inputs_author_must_differ_from_approver')
    if not IMAGE.fullmatch(str(image)) or not SHA.fullmatch(str(source_revision)):
        raise ValueError('wf10_inputs_image_rejected')
    config = deepcopy(facts['lab_config'])
    binding = config['bindings.json']['bindings'][0]
    policy = json.loads((PACKAGE_ROOT / 'config/runbook-policy.local.example.json').read_text())
    policy['targets'][0].update(alias=binding['alias'], target=dict(binding['target']))
    config['runbook-policy.json'] = policy
    plain = any(c.get('kind') == 'prometheus' and str(c.get('base_url', '')).startswith('http://')
                for c in config['runtime.json']['connections'].values())
    secrets = dict(SECRETS) if plain else {**SECRETS, 'collector': 'ai-ops-collector'}
    recovery = {'approvers': [o['user_id']],
                'approval': {'wait_seconds': 3600, 'approval_seconds': 600, 'observation_seconds': 30,
                             'revalidation_seconds': 10},
                'principals': {o['user_id']: o['approver_github_id']}, 'author_id': o['author_github_id'],
                'aws_role_name': o['aws_role_name'], 'secrets': dict(RECOVERY_SECRETS),
                'normal_binding': deepcopy(normal_binding)}
    if ci_relay:
        recovery['ci_relay'] = {'secret': CI_RELAY_SECRET}
    if publish:
        recovery['publish'] = {'secret': PUBLISH_SECRET}
    value = {**draft(), 'namespace': 'ai-ops', 'name': 'ai-ops', 'pvc_name': 'ai-ops-data', 'service_account': 'ai-ops',
             'image': image, 'source_revision': source_revision,
             'source': {'alias': 'eks-seoul', 'source_id': 'am-urlshortener', 'cluster_id': binding['target']['cluster_id']},
             'slack': {'team_id': o['team_id'], 'app_id': o['app_id'], 'bot_user_id': bot_user_id,
                       'user_id': o['user_id'], 'investigation_channel_id': o['investigation_channel_id'],
                       'alert_channel_id': alert_channel_id},
             'secrets': secrets,
             'network': {'dns': {'namespace': 'kube-system', 'labels': {'k8s-app': 'kube-dns'}},
                         'alertmanager': {'namespace': 'monitoring', 'labels': {'app.kubernetes.io/name': 'alertmanager'}},
                         'ingress': {'namespace': 'traefik', 'labels': {'app.kubernetes.io/name': 'traefik'}},
                         'collector_cidrs': collector_cidrs(facts)},
             'config': config, 'recovery': recovery}
    _, missing = inputs(value)
    if missing:
        raise ValueError('wf10_inputs_incomplete')
    result = candidate(value)  # full renderer validation (schema, policy, network, recovery)
    return value, {'status': result['status'], 'manifest_sha256': result['manifest_sha256'],
                   'resources': [[r['kind'], r['metadata']['name']] for r in result['resources']]}


def image_from_proof(proof):
    """(image, source_revision) from the AI image import proof (the imported, scanned digest)."""
    digest, revision = proof.get('registry_digest'), proof.get('source_revision')
    if not isinstance(digest, str) or not isinstance(revision, str) or proof.get('scan_findings') != 0:
        raise ValueError('wf10_inputs_image_proof_rejected')
    image = REGISTRY + '@' + digest
    if not IMAGE.fullmatch(image) or not SHA.fullmatch(revision):
        raise ValueError('wf10_inputs_image_proof_rejected')
    return image, revision


def _write_new(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description='WF10 inputs: read-only facts, then validated assembly')
    sub = parser.add_subparsers(dest='action', required=True)
    c = sub.add_parser('collect')
    c.add_argument('--kubeconfig', type=Path)
    c.add_argument('--output', type=Path, required=True)
    b = sub.add_parser('build')
    b.add_argument('--facts', type=Path, required=True)
    b.add_argument('--normal-binding', type=Path, required=True, help='prepare_b6_normal_ci output (uses .binding)')
    b.add_argument('--image-proof', type=Path, required=True, help='AI image import proof (registry_digest, source_revision)')
    b.add_argument('--bot-user-id', required=True)
    b.add_argument('--alert-channel-id', required=True)
    for key in SLACK_OVERRIDES:
        b.add_argument('--' + key.replace('_', '-'), help=f'default {DEFAULTS[key]}')
    b.add_argument('--no-ci-relay', action='store_true')
    b.add_argument('--no-publish', action='store_true')
    b.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == 'collect':
            facts = collect(kubectl_reader(args.kubeconfig))
            _write_new(args.output, facts)
            print(json.dumps({'status': 'collected', 'collector_cidrs': collector_cidrs(facts)}))
            return 0
        prepared = json.loads(args.normal_binding.read_text())
        image, revision = image_from_proof(json.loads(args.image_proof.read_text()))
        overrides = {k: getattr(args, k) for k in SLACK_OVERRIDES if getattr(args, k) is not None}
        value, summary = build(json.loads(args.facts.read_text()), bot_user_id=args.bot_user_id,
                               alert_channel_id=args.alert_channel_id, image=image, source_revision=revision,
                               normal_binding=prepared['binding'], ci_relay=not args.no_ci_relay,
                               publish=not args.no_publish, overrides=overrides)
        _write_new(args.output, value)
        print(json.dumps(summary))
        return 0
    except (ValueError, OSError, KeyError, TypeError, IndexError, AttributeError, subprocess.SubprocessError):
        print(json.dumps({'status': 'wf10_inputs_rejected', 'action': args.action}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
