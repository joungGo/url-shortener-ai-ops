"""Offline WF10 candidate renderer. Never applies resources or grants execution.

Required render inputs and later live approval evidence are separate: hashing a
candidate must not require its own hash as input. A complete candidate is not a
verified environment. No credentials are read by this module.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from ai_ops.k8s_schema import SchemaError, validate_manifest
from ai_ops.b_contracts import BindingRegistry, BRuntime, MetricCatalog, CollectionLimits, _json_object
from ai_ops.slack_adapter import SlackPolicy
from ai_ops.slack_runtime import SlackWebConfig
from ai_ops.slack_sender import PORT as SENDER_PORT
from ai_ops.slack_server import load_settings
from ai_ops.wf10_preparation import encoded, digest, read_regular, LIMITS, UNRESOLVED
from ai_ops.recovery_egress_proxy import PORT as EGRESS_PORT

CONFIG_NAMES = ('bindings.json', 'runtime.json', 'metric-queries.json', 'runbook-policy.json')
FIELDS = ('namespace', 'name', 'pvc_name', 'service_account', 'image', 'source_revision', 'source',
          'slack', 'secrets', 'network', 'config')
INIT_RESOURCES = {'requests':{'cpu':'10m','memory':'32Mi'},'limits':{'cpu':'100m','memory':'64Mi'}}
LIVE_GATES = ('current_identity_permissions', 'fresh_store_and_manual_policy',
              'same_physical_store_uid_binding', 'normal_ci_and_worker', 'tls_and_routes',
              'manifest_and_rollback_hashes', 'source_image_provenance', 'exact_connection_approval',
              'egress_enforcement_negative_tests', 'slack_token_ip_restriction', 'server_side_dry_run')
SECRET_KEYS = ('signing', 'bot', 'slack_tls', 'event_identity', 'collector', 'sender_client')
# The sender is the only internet-egress Pod. Private, CGNAT, loopback and
# link-local (incl. IMDS 169.254.169.254) ranges stay closed; the VPC CIDR must
# fall inside these exceptions (live gate).
NON_PUBLIC = ['0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
              '169.254.0.0/16', '172.16.0.0/12', '192.168.0.0/16']
# Optional WF07 recovery approval/preparation (A7). Absent -> the candidate is
# byte-identical to before. Present -> recovery-approval.json, the normal-CI
# binding, the relay's reader/GitHub secrets and the fixed-destination egress
# proxy Pod (the relay's only path to GitHub/STS/ECR, design B').
RECOVERY_FIELDS = ('approvers', 'approval', 'principals', 'author_id', 'aws_role_name', 'secrets',
                   'normal_binding')
APPROVAL_FIELDS = ('wait_seconds', 'approval_seconds', 'observation_seconds', 'revalidation_seconds')
RECOVERY_SECRET_KEYS = ('reader', 'github')
PACKAGE_LIMIT = 131072


def draft():
    return {'schema_version': 1, 'kind': 'wf10_candidate_inputs',
            **dict.fromkeys(FIELDS), 'execution_allowed': False}


def exact(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError('wf10_fields_rejected')


def match(value, pattern):
    if type(value) is not str or not re.fullmatch(pattern, value):
        raise ValueError('wf10_value_rejected')
    return value


def name(value):
    return match(value, r'[a-z][a-z0-9-]{0,50}[a-z0-9]|[a-z]')


def inputs(value):
    if type(value) is not dict:
        raise ValueError('wf10_fields_rejected')
    exact({k: x for k, x in value.items() if k != 'recovery'}, draft())
    if type(value['schema_version']) is not int or value['schema_version'] != 1 or value['kind'] != 'wf10_candidate_inputs' or value['execution_allowed'] is not False:
        raise ValueError('wf10_input_mode_rejected')
    missing = sorted(k for k in FIELDS if value[k] is None)
    if missing:
        return None, missing
    v = deepcopy(value)
    for k in ('namespace', 'name', 'pvc_name', 'service_account'):
        name(v[k])
    if v['service_account'] == 'default':
        raise ValueError('wf10_default_identity_rejected')
    match(v['source_revision'], r'[a-f0-9]{40}')
    match(v['image'], r'[a-z0-9][a-z0-9./:-]*@sha256:[a-f0-9]{64}')
    exact(v['source'], ('alias', 'source_id', 'cluster_id'))
    for k in v['source']:
        match(v['source'][k], r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
    exact(v['slack'], ('team_id', 'app_id', 'bot_user_id', 'user_id', 'investigation_channel_id', 'alert_channel_id'))
    s = v['slack']
    match(s['alert_channel_id'], r'[CG][A-Z0-9]{2,31}')
    if s['alert_channel_id'] == s['investigation_channel_id']:
        raise ValueError('wf10_channel_boundary_rejected')
    exact(v['secrets'], SECRET_KEYS)
    for x in v['secrets'].values():
        name(x)
    if len(set(v['secrets'].values())) != len(SECRET_KEYS):
        raise ValueError('wf10_credential_separation_rejected')
    # Slack egress is not an IP input: Slack publishes no fixed addresses (B' sender).
    exact(v['network'], ('dns', 'alertmanager', 'ingress', 'collector_cidrs'))
    for k in ('dns', 'alertmanager', 'ingress'):
        peer = v['network'][k]
        exact(peer, ('namespace', 'labels')); name(peer['namespace'])
        if type(peer['labels']) is not dict or not peer['labels'] or len(peer['labels']) > 8:
            raise ValueError('wf10_selector_rejected')
        for key, val in peer['labels'].items():
            match(key, r'[a-zA-Z0-9][a-zA-Z0-9./_-]{0,62}')
            match(val, r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}')
    cidrs = v['network']['collector_cidrs']
    if type(cidrs) is not list or not 1 <= len(cidrs) <= 16 or len(set(cidrs)) != len(cidrs):
        raise ValueError('wf10_network_rejected')
    for text in cidrs:
        net = ipaddress.ip_network(text, strict=True)
        if net.version != 4 or net.prefixlen != 32 or net.is_loopback or net.is_link_local:
            raise ValueError('wf10_network_rejected')
    exact(v['config'], CONFIG_NAMES)
    if v.get('recovery') is not None:
        recovery_config(v)  # validates the whole optional section before any write
    # Validate schemas BEFORE writing any temporary config. Do not echo input.
    registry = BindingRegistry.model_validate(v['config']['bindings.json'])
    runtime = BRuntime.model_validate(v['config']['runtime.json'])
    MetricCatalog.model_validate(v['config']['metric-queries.json'])
    if len(registry.bindings) != 1 or runtime.llm_enabled:
        raise ValueError('wf10_scope_rejected')
    b = registry.bindings[0]
    if b.alias != 'url-shortener-b6-lab' or b.target.environment != 'practice' or b.target.namespace != 'ai-ops-lab' or b.target.workload != 'lab-api' or b.target.cluster_id != v['source']['cluster_id']:
        raise ValueError('wf10_target_rejected')
    if runtime.limits != CollectionLimits():
        raise ValueError('wf10_collection_limits_changed')
    if set(runtime.connections) != {b.kubernetes_connection, b.prometheus_connection}:
        raise ValueError('wf10_connections_rejected')
    for conn in runtime.connections.values():
        prefix = '/var/run/ai-ops/kubernetes' if conn.kind == 'kubernetes' else '/run/collector'
        if not conn.base_url or (urlsplit(conn.base_url).port or 443) not in (443,6443,8443,9090) or not conn.base_url.startswith('https://') or conn.token_file != prefix+'/token' or conn.ca_file != prefix+'/ca.crt':
            raise ValueError('wf10_collector_paths_rejected')
    exact(v['config']['runbook-policy.json'], ('schema_version', 'registry_sha256', 'metadata_sha256', 'targets'))
    policy(v)  # Validates every Slack identifier and grant.
    web(v)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for f, obj in v['config'].items():
            data = encoded(obj)
            if len(data) > 65536: raise ValueError('wf10_config_size_rejected')
            (root/f).write_bytes(data)
        (root/'slack-policy.json').write_bytes(encoded(policy(v)))
        settings, _ = load_settings(root)
    return v, []


def policy(v):
    s = v['slack']
    return SlackPolicy(team_id=s['team_id'], app_id=s['app_id'], bot_user_id=s['bot_user_id'],
        grants=[{'user_id':s['user_id'], 'channel_id':s['investigation_channel_id'],
                 'targets':['url-shortener-b6-lab']}]).model_dump()


def web(v):
    source = v['source']
    # Relay holds no bot token: it calls the in-namespace sender Service.
    sender = {'url':f"http://{v['name']}-sender.{v['namespace']}.svc:{SENDER_PORT}",
              'client_secret_file':'/run/slack-sender-client/token'}
    return SlackWebConfig(enabled=True, sender=sender,
        signing_secret_file='/run/slack-signing/token',
        event_sources={source['alias']:{k:source[k] for k in ('source_id', 'cluster_id')}}).model_dump()


def recovery_config(v):
    """recovery-approval.json for the optional section, validated by the runtime models."""
    from ai_ops.normal_ci_contract import NormalCIBinding
    from ai_ops.recovery_approval_wiring import RecoveryApprovalConfig
    r = v['recovery']
    exact(r, RECOVERY_FIELDS)
    exact(r['approval'], APPROVAL_FIELDS)
    exact(r['secrets'], RECOVERY_SECRET_KEYS)
    for x in r['secrets'].values():
        name(x)
    if len(set(r['secrets'].values()) | set(v['secrets'].values())) != len(RECOVERY_SECRET_KEYS) + len(SECRET_KEYS):
        raise ValueError('wf10_credential_separation_rejected')
    # Approvers must be able to press the buttons: the rendered policy grants one user.
    if type(r['approvers']) is not list or not r['approvers'] or set(r['approvers']) != {v['slack']['user_id']}:
        raise ValueError('wf10_recovery_approvers_rejected')
    NormalCIBinding.model_validate(r['normal_binding'])
    kubernetes = [c for c in v['config']['runtime.json']['connections'].values() if c['kind'] == 'kubernetes']
    if len(kubernetes) != 1:
        raise ValueError('wf10_recovery_api_rejected')
    proxy = f"http://{v['name']}-egress.{v['namespace']}.svc:{EGRESS_PORT}"
    return RecoveryApprovalConfig.model_validate({
        'schema_version': 1, 'enabled': True,
        'prepare_proposal': {'version': 1, 'scope': 'prepare_proposal', 'approvers': r['approvers'], **r['approval']},
        # Same API server/egress rule as the existing Kubernetes collector, separate read identity.
        'observe': {'api_url': kubernetes[0]['base_url'], 'token_file': '/run/recovery-reader/token',
                    'ca_file': '/run/recovery-reader/ca.crt', 'argo_namespace': 'argocd',
                    'argo_application': 'lab-target'},
        'prepare': {'principals': r['principals'], 'author_id': r['author_id'],
                    'drafts_dir': '/data/recovery-drafts', 'github_token_file': '/run/ai-github/token',
                    'normal_binding_file': '/etc/ai-ops/normal-ci-binding.json',
                    'aws_role_name': r['aws_role_name'], 'egress_proxy': proxy}}).model_dump()


def candidate(value):
    v, missing = inputs(value)
    if missing:
        return {'status':'blocked_missing_inputs', 'missing':missing,
                'unresolved_live_inputs':sorted(UNRESOLVED), 'execution_allowed':False, 'resources':[]}
    ns, n = v['namespace'], v['name']
    labels = {'app.kubernetes.io/name':n, 'app.kubernetes.io/part-of':'wf10'}
    sender_labels = {'app.kubernetes.io/name':n+'-sender', 'app.kubernetes.io/part-of':'wf10'}
    def obj(kind, suffix, spec, api='v1'):
        return {'apiVersion':api, 'kind':kind, 'metadata':{'name':n+suffix,'namespace':ns}, **spec}
    configs = {**v['config'], 'slack-policy.json':policy(v), 'slack-web.json':web(v)}
    recovery = v.get('recovery')
    egress_labels = {'app.kubernetes.io/name':n+'-egress', 'app.kubernetes.io/part-of':'wf10'}
    if recovery is not None:
        configs['recovery-approval.json'] = recovery_config(v)
        configs['normal-ci-binding.json'] = recovery['normal_binding']
    config_data = {f:encoded(x).decode() for f,x in configs.items()}
    config_hash = digest(encoded(config_data))
    security = {'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}}
    def mount(vol, path, ro=False): return {'name':vol,'mountPath':path,'readOnly':ro}
    common = [mount('config','/etc/ai-ops',True), mount('data','/data')]
    volumes = [{'name':'config','configMap':{'name':n+'-config'}},
               {'name':'data','persistentVolumeClaim':{'claimName':v['pvc_name']}}]
    # Per-role private emptyDir: secret-volume symlinks/modes are not accepted by
    # existing Slack readers. Copy only fixed keys as uid10001/mode0600 in init.
    containers, init = [], []
    def secret(vol, ref, keys, destination, volumes=volumes, init=init):
        volumes.extend([{'name':vol+'-source','secret':{'secretName':ref,'defaultMode':288,
            'items':[{'key':k,'path':k} for k in keys]}}, {'name':vol,'emptyDir':{'medium':'Memory','sizeLimit':'1Mi'}}])
        script = 'import os,pathlib; os.umask(0o077); '+ '; '.join(
            f'p=pathlib.Path("/out/{k}"); p.write_bytes(pathlib.Path("/source/{k}").read_bytes()); p.chmod(0o600)' for k in keys)
        # Explicit bounds so LimitRange/ResourceQuota namespaces accept the Pod.
        init.append({'name':'copy-'+vol,'image':v['image'],'command':['python','-c',script],
                     'securityContext':security,'resources':INIT_RESOURCES,
                     'volumeMounts':[mount(vol+'-source','/source',True),mount(vol,'/out')]})
        return mount(vol,destination,True)
    signing=secret('signing',v['secrets']['signing'],['token'],'/run/slack-signing')
    client=secret('sender-client',v['secrets']['sender_client'],['token'],'/run/slack-sender-client')
    tls=secret('slack-tls',v['secrets']['slack_tls'],['tls.crt','tls.key'],'/run/ai-slack-tls')
    event=secret('event-identity',v['secrets']['event_identity'],['token','tls.crt','tls.key'],'/etc/ai-ops-webhook')
    collector=secret('collector',v['secrets']['collector'],['token','ca.crt'],'/run/collector')
    relay_extra=[]
    if recovery is not None:
        # Only the relay reads the cluster for recovery and calls GitHub (read token).
        relay_extra=[secret('recovery-reader',recovery['secrets']['reader'],['token','ca.crt'],'/run/recovery-reader'),
                     secret('github',recovery['secrets']['github'],['token'],'/run/ai-github')]
    volumes.append({'name':'kubernetes','projected':{'defaultMode':288,'sources':[
        {'serviceAccountToken':{'path':'token','expirationSeconds':3600}},
        {'configMap':{'name':'kube-root-ca.crt','items':[{'key':'ca.crt','path':'ca.crt'}]}}]}})
    source=v['source']
    roles=[('worker','ai_ops.worker_runtime',[],[collector,mount('kubernetes','/var/run/ai-ops/kubernetes',True)]),
        ('event-receiver','ai_ops.alert_receiver',['--cluster-id',source['cluster_id'],'--event-source-alias',source['alias'],'--event-source-id',source['source_id']],[event]),
        ('slack-ingress','ai_ops.slack_server',['ingress','--enable-web'],[signing,tls]),
        ('slack-relay','ai_ops.slack_server',['relay','--enable-web'],[signing,client,*relay_extra])]
    for role,module,args,extra in roles:
        volumes.append({'name':role+'-tmp','emptyDir':{'sizeLimit':'64Mi'}})
        container={'name':role,'image':v['image'],'imagePullPolicy':'IfNotPresent',
          'command':['python','-m',module],'args':args,'securityContext':security,
          'env':[{'name':'PYTHONDONTWRITEBYTECODE','value':'1'},{'name':'HOME','value':'/tmp'},
                 {'name':'AI_OPS_RUNBOOK_ROOT','value':'/opt/ai-ops/runbooks'}],
          'resources':{'requests':{'cpu':'50m','memory':'128Mi'},'limits':{'cpu':'1','memory':'1Gi'}},
          'volumeMounts':common+extra+[mount(role+'-tmp','/tmp')]}
        if role=='event-receiver':
            probe={'tcpSocket':{'port':9443}}
        else:
            action=['--probe'] if role=='worker' else ['probe-'+role.removeprefix('slack-')]
            probe={'exec':{'command':['python','-m',module,*action]}}
        container['readinessProbe']={**probe,'periodSeconds':10,'timeoutSeconds':5}
        container['startupProbe']={**probe,'periodSeconds':10,'timeoutSeconds':5,'failureThreshold':12}
        # Same contract as the existing chart/ci_service: restart a stalled process
        # after ~90s; heartbeat limits are worker 120s and Slack 30s.
        container['livenessProbe']={**probe,'periodSeconds':30,'timeoutSeconds':5,'failureThreshold':3}
        containers.append(container)
    pod={'serviceAccountName':v['service_account'],'automountServiceAccountToken':False,
         'enableServiceLinks':False,'terminationGracePeriodSeconds':90,
         # OnRootMismatch: the default recursive fsGroup pass widens the shared
         # existing PVC store to 0660/2770 and breaks the queue 0600/0700 checks.
         'securityContext':{'runAsNonRoot':True,'runAsUser':10001,'runAsGroup':10001,'fsGroup':10001,
            'fsGroupChangePolicy':'OnRootMismatch','seccompProfile':{'type':'RuntimeDefault'}},'containers':containers,'initContainers':init,'volumes':volumes}
    resources=[obj('ConfigMap','-config',{'data':config_data}),
        obj('Deployment','',{'spec':{'replicas':1,'strategy':{'type':'Recreate'},'selector':{'matchLabels':labels},
            'template':{'metadata':{'labels':labels,'annotations':{'checksum/config':config_hash}},'spec':pod}}},'apps/v1')]
    for suffix,port in (('-events',9443),('-slack',9444)):
        resources.append(obj('Service',suffix,{'spec':{'type':'ClusterIP','selector':labels,
            'ports':[{'name':'https','protocol':'TCP','port':port,'targetPort':port}]}}))
    def peer(x):return {'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':x['namespace']}},'podSelector':{'matchLabels':x['labels']}}
    net=v['network']
    resources.append(obj('NetworkPolicy','',{'spec':{'podSelector':{'matchLabels':labels},'policyTypes':['Ingress','Egress'],
        'ingress':[{'from':[peer(net[k])],'ports':[{'protocol':'TCP','port':port}]} for k,port in [('alertmanager',9443),('ingress',9444)]],
        'egress':[{'to':[peer(net['dns'])],'ports':[{'protocol':p,'port':53} for p in ('TCP','UDP')]},
                  {'to':[{'ipBlock':{'cidr':c}} for c in net['collector_cidrs']],
                   'ports':[{'protocol':'TCP','port':p} for p in sorted({urlsplit(c['base_url']).port or 443 for c in v['config']['runtime.json']['connections'].values()})]},
                  {'to':[{'podSelector':{'matchLabels':sender_labels}}],'ports':[{'protocol':'TCP','port':SENDER_PORT}]},
                  *([{'to':[{'podSelector':{'matchLabels':egress_labels}}],'ports':[{'protocol':'TCP','port':EGRESS_PORT}]}]
                    if recovery is not None else [])]}},'networking.k8s.io/v1'))
    # Separate stateless sender: bot token + client secret only. No PVC, no
    # Kubernetes token, no signing secret; only the policy file from the ConfigMap.
    s_volumes=[{'name':'policy','configMap':{'name':n+'-config','items':[{'key':'slack-policy.json','path':'slack-policy.json'}]}},
               {'name':'sender-tmp','emptyDir':{'sizeLimit':'16Mi'}}]
    s_init=[]
    s_mounts=[mount('policy','/etc/ai-ops',True),
              secret('bot',v['secrets']['bot'],['token'],'/run/slack-bot',s_volumes,s_init),
              secret('sender-client',v['secrets']['sender_client'],['token'],'/run/slack-sender-client',s_volumes,s_init),
              mount('sender-tmp','/tmp')]
    s_probe={'tcpSocket':{'port':SENDER_PORT}}
    sender={'name':'slack-sender','image':v['image'],'imagePullPolicy':'IfNotPresent',
        'command':['python','-m','ai_ops.slack_sender'],'args':['--enable-web'],'securityContext':security,
        'env':[{'name':'PYTHONDONTWRITEBYTECODE','value':'1'},{'name':'HOME','value':'/tmp'}],
        'resources':{'requests':{'cpu':'25m','memory':'64Mi'},'limits':{'cpu':'250m','memory':'256Mi'}},
        'volumeMounts':s_mounts,
        'readinessProbe':{**s_probe,'periodSeconds':10,'timeoutSeconds':5},
        'startupProbe':{**s_probe,'periodSeconds':5,'timeoutSeconds':5,'failureThreshold':12},
        'livenessProbe':{**s_probe,'periodSeconds':30,'timeoutSeconds':5,'failureThreshold':3}}
    s_pod={'serviceAccountName':v['service_account'],'automountServiceAccountToken':False,
         'enableServiceLinks':False,'terminationGracePeriodSeconds':30,
         'securityContext':{'runAsNonRoot':True,'runAsUser':10001,'runAsGroup':10001,'fsGroup':10001,
            'seccompProfile':{'type':'RuntimeDefault'}},'containers':[sender],'initContainers':s_init,'volumes':s_volumes}
    # Recreate + 1 replica: one in-memory idempotency window, never two senders.
    resources.append(obj('Deployment','-sender',{'spec':{'replicas':1,'strategy':{'type':'Recreate'},'selector':{'matchLabels':sender_labels},
        'template':{'metadata':{'labels':sender_labels,'annotations':{'checksum/config':config_hash}},'spec':s_pod}}},'apps/v1'))
    resources.append(obj('Service','-sender',{'spec':{'type':'ClusterIP','selector':sender_labels,
        'ports':[{'name':'http','protocol':'TCP','port':SENDER_PORT,'targetPort':SENDER_PORT}]}}))
    resources.append(obj('NetworkPolicy','-sender',{'spec':{'podSelector':{'matchLabels':sender_labels},'policyTypes':['Ingress','Egress'],
        'ingress':[{'from':[{'podSelector':{'matchLabels':labels}}],'ports':[{'protocol':'TCP','port':SENDER_PORT}]}],
        'egress':[{'to':[peer(net['dns'])],'ports':[{'protocol':p,'port':53} for p in ('TCP','UDP')]},
                  {'to':[{'ipBlock':{'cidr':'0.0.0.0/0','except':NON_PUBLIC}}],'ports':[{'protocol':'TCP','port':443}]}]}},'networking.k8s.io/v1'))
    if recovery is not None:
        # Fixed-destination CONNECT proxy: no secrets, no Kubernetes token, no PVC.
        # TLS stays end to end; only GitHub/STS/ECR :443 (ai_ops.recovery_egress_proxy).
        e_probe={'tcpSocket':{'port':EGRESS_PORT}}
        egress={'name':'egress-proxy','image':v['image'],'imagePullPolicy':'IfNotPresent',
            'command':['python','-m','ai_ops.recovery_egress_proxy'],'args':[],'securityContext':security,
            'env':[{'name':'PYTHONDONTWRITEBYTECODE','value':'1'},{'name':'HOME','value':'/tmp'}],
            'resources':{'requests':{'cpu':'25m','memory':'32Mi'},'limits':{'cpu':'250m','memory':'128Mi'}},
            'volumeMounts':[mount('egress-tmp','/tmp')],
            'readinessProbe':{**e_probe,'periodSeconds':10,'timeoutSeconds':5},
            'startupProbe':{**e_probe,'periodSeconds':5,'timeoutSeconds':5,'failureThreshold':12},
            'livenessProbe':{**e_probe,'periodSeconds':30,'timeoutSeconds':5,'failureThreshold':3}}
        e_pod={'serviceAccountName':v['service_account'],'automountServiceAccountToken':False,
             'enableServiceLinks':False,'terminationGracePeriodSeconds':30,
             'securityContext':{'runAsNonRoot':True,'runAsUser':10001,'runAsGroup':10001,
                'seccompProfile':{'type':'RuntimeDefault'}},'containers':[egress],
             'volumes':[{'name':'egress-tmp','emptyDir':{'sizeLimit':'16Mi'}}]}
        resources.append(obj('Deployment','-egress',{'spec':{'replicas':1,'strategy':{'type':'Recreate'},'selector':{'matchLabels':egress_labels},
            'template':{'metadata':{'labels':egress_labels},'spec':e_pod}}},'apps/v1'))
        resources.append(obj('Service','-egress',{'spec':{'type':'ClusterIP','selector':egress_labels,
            'ports':[{'name':'connect','protocol':'TCP','port':EGRESS_PORT,'targetPort':EGRESS_PORT}]}}))
        resources.append(obj('NetworkPolicy','-egress',{'spec':{'podSelector':{'matchLabels':egress_labels},'policyTypes':['Ingress','Egress'],
            'ingress':[{'from':[{'podSelector':{'matchLabels':labels}}],'ports':[{'protocol':'TCP','port':EGRESS_PORT}]}],
            'egress':[{'to':[peer(net['dns'])],'ports':[{'protocol':p,'port':53} for p in ('TCP','UDP')]},
                      {'to':[{'ipBlock':{'cidr':'0.0.0.0/0','except':NON_PUBLIC}}],'ports':[{'protocol':'TCP','port':443}]}]}},'networking.k8s.io/v1'))
    # Offline v1.35 structural check (strict unknown fields). Never emit a manifest
    # the API server would reject on shape; server dry-run remains a live gate.
    for resource in resources:
        try:
            validate_manifest(resource)
        except SchemaError:
            raise ValueError('wf10_schema_rejected') from None
    result={'status':'candidate_rendered_not_approved','execution_allowed':False,'live_verified':False,
        'schema_validation':{'kubernetes':'v1.35.8','mode':'offline_openapi_subset_strict','server_dry_run':False},
        'source_revision':v['source_revision'],'preserved_c_limits_seconds':LIMITS,'manual_policy':{'rules':[],'strategy':'new_empty_active_queue_archive_old_read_only'},
        'required_live_gates':list(LIVE_GATES),'unresolved_live_inputs':sorted(UNRESOLVED),
        'external_prerequisites':['existing namespace/PVC/least-read-only service account and secrets incl. a 32-hex sender client secret',
            'new empty active store; stop old workers and reconcile old jobs/sends before cutover',
            'fresh source registry/UID/TLS/CI and additive NetworkPolicy audit',
            'existing ingress/DNS and Alertmanager direct notice preservation, no automatic route change',
            'platform: VPC CNI network policy strict enforcing mode, IMDSv2 hop limit 1, VPC CIDR inside sender egress exceptions',
            'Slack app token IP restriction to the preserved NAT egress IP; minimal bot scopes'],
        'residual_risks':['slack.com itself remains an exfiltration path for a compromised sender',
            'DNS queries via cluster DNS remain an exfiltration channel',
            'main Pod to sender is in-cluster plain HTTP with a bearer client secret',
            'sender idempotency window is in memory; restarts rely on the adapter needs_review rule',
            'single replica main Pod and SQLite on one RWO volume: no HA'],
        'manifest_sha256':digest(encoded(resources)),'resources':resources}
    if recovery is not None:
        result['recovery']={'enabled':True,'egress_proxy':configs['recovery-approval.json']['prepare']['egress_proxy'],
                            'publication':'not_wired'}
        result['external_prerequisites']=result['external_prerequisites']+[
            'recovery reader identity (secret): deployments get, replicasets list, pods list, leases get in ai-ops-lab; Argo applications get lab-target; no writes',
            'read-only GitHub token (secret) for joungGo/c03-execution-lab Contents: Read',
            'IRSA: role-arn annotation on the main service account, role allowing only ecr:DescribeImages on repository urlshortener',
            'CI relay running against the same store with this normal-CI binding (receipts within 15 minutes of each run)',
            'Slack app Interactivity Request URL /slack/interactivity on the existing Slack listener']
        result['residual_risks']=result['residual_risks']+[
            'the egress proxy relays TLS to GitHub/STS/ECR: a compromised relay could reach those three hosts',
            'IRSA environment is injected into every main-Pod container, not only the relay']
    return result


def build(value, directory):
    result=candidate(value)
    if not result['resources']:return result
    payloads={'inputs.json':encoded(value),'candidate.json':encoded(result)}
    # Candidate packages may carry the optional recovery section (~70KB); still bounded.
    if any(len(x)>PACKAGE_LIMIT for x in payloads.values()):raise ValueError('wf10_package_too_large')
    root=Path(directory);root.mkdir(mode=0o700,exist_ok=False)
    for name,content in payloads.items():
        fd=os.open(root/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(content)
    return {'status':result['status'],'manifest_sha256':result['manifest_sha256'],'execution_allowed':False}


def verify(directory):
    root=Path(directory)
    if root.is_symlink() or {x.name for x in root.iterdir()} != {'inputs.json','candidate.json'}:raise ValueError('wf10_package_rejected')
    expected=candidate(_json_object(read_regular(root/'inputs.json',PACKAGE_LIMIT)))
    if not expected['resources'] or encoded(expected)!=read_regular(root/'candidate.json',PACKAGE_LIMIT):raise ValueError('wf10_candidate_changed')
    return {'status':'candidate_verified_not_approved','execution_allowed':False,'live_verified':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description='Offline WF10 configuration candidates; never apply')
    sub=parser.add_subparsers(dest='action',required=True)
    b=sub.add_parser('build');b.add_argument('--input',type=Path,required=True);b.add_argument('--output',type=Path,required=True)
    v=sub.add_parser('verify');v.add_argument('--directory',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        result=build(_json_object(read_regular(args.input)),args.output) if args.action=='build' else verify(args.directory)
        print(json.dumps(result,sort_keys=True));return 2 if result['status']=='blocked_missing_inputs' else 0
    except (ValueError,TypeError,KeyError,OSError,RecursionError):
        print('{"status":"rejected","execution_allowed":false}');return 1

if __name__=='__main__':raise SystemExit(main())
