"""WF11 pre-install manifests that the WF10 renderer treats as existing inputs (no secret values).

Rendered from the SAME WF10 candidate input as ``wf10_deployment.candidate`` so names
cannot drift, plus two install facts (IRSA role ARN, Slack host). Offline only: no
kubectl, no cluster, no credentials. Output is reviewed and applied by the operator.

* WF10 ServiceAccount ``<service_account>`` with the IRSA role annotation (PR73 role);
* recovery reader identity: ServiceAccount, read-only Roles/RoleBindings in the lab and
  Argo namespaces, and a ``kubernetes.io/service-account-token`` Secret named like the
  renderer's ``recovery.secrets.reader`` (keys token/ca.crt are filled by Kubernetes);
* Slack edge (2026-11-17 shape): cert-manager Certificate for the Slack host into the
  renderer's ``secrets.slack_tls`` Secret, and a Traefik IngressRouteTCP passthrough
  to ``<name>-slack:9444``.

Event-receiver TLS/token and other secret material are a separate step.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from ai_ops.wf10_deployment import inputs

NAME = re.compile(r'[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?')
HOST = re.compile(r'([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}')
ROLE_ARN = re.compile(r'arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_-]{1,64}')
LAB_NAMESPACE, LAB_DEPLOYMENT, LAB_LEASE = 'ai-ops-lab', 'lab-api', 'b6-scenario-lock'
ARGO_NAMESPACE, ARGO_APPLICATION = 'argocd', 'lab-target'
ISSUER = 'letsencrypt-traefik'
SLACK_PORT = 9444
PART_OF = {'app.kubernetes.io/part-of': 'wf10', 'ai-ops.connection-plan': 'wf11'}


def _meta(name, namespace, **extra):
    return {'name': name, 'namespace': namespace, 'labels': dict(PART_OF), **extra}


def reader_rules():
    """Exactly the reads of the observer/proof/healthy paths (WF12 checklist), no writes."""
    lab = [
        {'apiGroups': ['apps'], 'resources': ['deployments'], 'resourceNames': [LAB_DEPLOYMENT], 'verbs': ['get']},
        {'apiGroups': ['apps'], 'resources': ['replicasets'], 'verbs': ['list']},
        {'apiGroups': [''], 'resources': ['pods'], 'verbs': ['list']},
        # Pod names change on every rollout, so proxy reads cannot be name-pinned.
        {'apiGroups': [''], 'resources': ['pods/proxy'], 'verbs': ['get']},
        {'apiGroups': ['coordination.k8s.io'], 'resources': ['leases'], 'resourceNames': [LAB_LEASE], 'verbs': ['get']},
    ]
    argo = [{'apiGroups': ['argoproj.io'], 'resources': ['applications'], 'resourceNames': [ARGO_APPLICATION],
             'verbs': ['get']}]
    return lab, argo


def manifests(value, *, role_arn, slack_host):
    v, missing = inputs(value)
    if missing:
        raise ValueError('wf11_edge_inputs_incomplete')
    if v.get('recovery') is None:
        raise ValueError('wf11_edge_recovery_section_required')
    if not isinstance(role_arn, str) or not ROLE_ARN.fullmatch(role_arn):
        raise ValueError('wf11_edge_role_arn_rejected')
    if not isinstance(slack_host, str) or not HOST.fullmatch(slack_host) or len(slack_host) > 253:
        raise ValueError('wf11_edge_slack_host_rejected')
    ns, n, sa = v['namespace'], v['name'], v['service_account']
    reader_secret = v['recovery']['secrets']['reader']
    reader_sa = n + '-recovery-reader'
    for name in (ns, n, sa, reader_secret, reader_sa, v['secrets']['slack_tls']):
        if not NAME.fullmatch(name):
            raise ValueError('wf11_edge_name_rejected')
    lab, argo = reader_rules()
    subject = [{'kind': 'ServiceAccount', 'name': reader_sa, 'namespace': ns}]
    return [
        {'apiVersion': 'v1', 'kind': 'ServiceAccount',
         'metadata': _meta(sa, ns, annotations={'eks.amazonaws.com/role-arn': role_arn}),
         'automountServiceAccountToken': False},
        {'apiVersion': 'v1', 'kind': 'ServiceAccount', 'metadata': _meta(reader_sa, ns),
         'automountServiceAccountToken': False},
        {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'kubernetes.io/service-account-token',
         'metadata': _meta(reader_secret, ns, annotations={'kubernetes.io/service-account.name': reader_sa})},
        {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'Role',
         'metadata': _meta(n + '-recovery-read', LAB_NAMESPACE), 'rules': lab},
        {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'RoleBinding',
         'metadata': _meta(n + '-recovery-read', LAB_NAMESPACE), 'subjects': subject,
         'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': n + '-recovery-read'}},
        {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'Role',
         'metadata': _meta(n + '-recovery-argo-read', ARGO_NAMESPACE), 'rules': argo},
        {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'RoleBinding',
         'metadata': _meta(n + '-recovery-argo-read', ARGO_NAMESPACE), 'subjects': subject,
         'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': n + '-recovery-argo-read'}},
        {'apiVersion': 'cert-manager.io/v1', 'kind': 'Certificate', 'metadata': _meta(n + '-slack-tls', ns),
         'spec': {'secretName': v['secrets']['slack_tls'], 'dnsNames': [slack_host],
                  'issuerRef': {'name': ISSUER, 'kind': 'ClusterIssuer', 'group': 'cert-manager.io'}}},
        {'apiVersion': 'traefik.io/v1alpha1', 'kind': 'IngressRouteTCP',
         'metadata': _meta(n + '-slack', ns, annotations={'kubernetes.io/ingress.class': 'traefik'}),
         'spec': {'entryPoints': ['websecure'],
                  'routes': [{'match': f'HostSNI(`{slack_host}`)', 'services': [{'name': n + '-slack', 'port': SLACK_PORT}]}],
                  'tls': {'passthrough': True}}},
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description='WF11 pre-install edge/identity manifests (offline, no secrets)')
    parser.add_argument('--inputs', type=Path, required=True, help='the WF10 candidate input JSON')
    parser.add_argument('--role-arn', required=True, help='PR73 output wf11_recovery_role_arn')
    parser.add_argument('--slack-host', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        documents = manifests(json.loads(args.inputs.read_text()), role_arn=args.role_arn, slack_host=args.slack_host)
        with args.output.open('x') as stream:  # never overwrite a reviewed file
            json.dump({'apiVersion': 'v1', 'kind': 'List', 'items': documents}, stream, ensure_ascii=False, indent=2)
        print(json.dumps({'status': 'written', 'resources': [[d['kind'], d['metadata']['namespace'],
                                                              d['metadata']['name']] for d in documents]}))
        return 0
    except (ValueError, OSError, KeyError, TypeError):
        print(json.dumps({'status': 'wf11_edge_rejected'}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
