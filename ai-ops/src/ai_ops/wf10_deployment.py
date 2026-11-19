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

from ai_ops.b_contracts import BindingRegistry, BRuntime, MetricCatalog, CollectionLimits, _json_object
from ai_ops.slack_adapter import SlackPolicy
from ai_ops.slack_runtime import SlackWebConfig
from ai_ops.slack_server import load_settings
from ai_ops.wf10_preparation import encoded, digest, read_regular, LIMITS, UNRESOLVED

CONFIG_NAMES = ('bindings.json', 'runtime.json', 'metric-queries.json', 'runbook-policy.json')
FIELDS = ('namespace', 'name', 'pvc_name', 'service_account', 'image', 'source_revision', 'source',
          'slack', 'secrets', 'network', 'config')
LIVE_GATES = ('current_identity_permissions', 'fresh_store_and_manual_policy',
              'same_physical_store_uid_binding', 'normal_ci_and_worker', 'tls_and_routes',
              'manifest_and_rollback_hashes', 'source_image_provenance', 'exact_connection_approval')


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
    exact(value, draft())
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
    exact(v['secrets'], ('signing', 'bot', 'slack_tls', 'event_identity', 'collector'))
    for x in v['secrets'].values():
        name(x)
    if len(set(v['secrets'].values())) != 5:
        raise ValueError('wf10_credential_separation_rejected')
    exact(v['network'], ('dns', 'alertmanager', 'ingress', 'collector_cidrs', 'slack_cidrs'))
    for k in ('dns', 'alertmanager', 'ingress'):
        peer = v['network'][k]
        exact(peer, ('namespace', 'labels')); name(peer['namespace'])
        if type(peer['labels']) is not dict or not peer['labels'] or len(peer['labels']) > 8:
            raise ValueError('wf10_selector_rejected')
        for key, val in peer['labels'].items():
            match(key, r'[a-zA-Z0-9][a-zA-Z0-9./_-]{0,62}')
            match(val, r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}')
    for k in ('collector_cidrs', 'slack_cidrs'):
        cidrs = v['network'][k]
        if type(cidrs) is not list or not 1 <= len(cidrs) <= 16 or len(set(cidrs)) != len(cidrs):
            raise ValueError('wf10_network_rejected')
        for text in cidrs:
            net = ipaddress.ip_network(text, strict=True)
            if net.version != 4 or net.prefixlen != 32 or net.is_loopback or net.is_link_local or (k == 'slack_cidrs' and not net.is_global):
                raise ValueError('wf10_network_rejected')
    exact(v['config'], CONFIG_NAMES)
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
    return SlackWebConfig(enabled=True, bot_token_file='/run/slack-bot/token',
        signing_secret_file='/run/slack-signing/token',
        event_sources={source['alias']:{k:source[k] for k in ('source_id', 'cluster_id')}}).model_dump()


def candidate(value):
    v, missing = inputs(value)
    if missing:
        return {'status':'blocked_missing_inputs', 'missing':missing,
                'unresolved_live_inputs':sorted(UNRESOLVED), 'execution_allowed':False, 'resources':[]}
    ns, n = v['namespace'], v['name']
    labels = {'app.kubernetes.io/name':n, 'app.kubernetes.io/part-of':'wf10'}
    def obj(kind, suffix, spec, api='v1'):
        return {'apiVersion':api, 'kind':kind, 'metadata':{'name':n+suffix,'namespace':ns}, **spec}
    configs = {**v['config'], 'slack-policy.json':policy(v), 'slack-web.json':web(v)}
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
    def secret(vol, ref, keys, destination):
        volumes.extend([{'name':vol+'-source','secret':{'secretName':ref,'defaultMode':288,
            'items':[{'key':k,'path':k} for k in keys]}}, {'name':vol,'emptyDir':{'medium':'Memory','sizeLimit':'1Mi'}}])
        script = 'import os,pathlib; os.umask(0o077); '+ '; '.join(
            f'p=pathlib.Path("/out/{k}"); p.write_bytes(pathlib.Path("/source/{k}").read_bytes()); p.chmod(0o600)' for k in keys)
        init.append({'name':'copy-'+vol,'image':v['image'],'command':['python','-c',script],
                     'securityContext':security,'volumeMounts':[mount(vol+'-source','/source',True),mount(vol,'/out')]})
        return mount(vol,destination,True)
    signing=secret('signing',v['secrets']['signing'],['token'],'/run/slack-signing')
    bot=secret('bot',v['secrets']['bot'],['token'],'/run/slack-bot')
    tls=secret('slack-tls',v['secrets']['slack_tls'],['tls.crt','tls.key'],'/run/ai-slack-tls')
    event=secret('event-identity',v['secrets']['event_identity'],['token','tls.crt','tls.key'],'/etc/ai-ops-webhook')
    collector=secret('collector',v['secrets']['collector'],['token','ca.crt'],'/run/collector')
    volumes.append({'name':'kubernetes','projected':{'defaultMode':288,'sources':[
        {'serviceAccountToken':{'path':'token','expirationSeconds':3600}},
        {'configMap':{'name':'kube-root-ca.crt','items':[{'key':'ca.crt','path':'ca.crt'}]}}]}})
    source=v['source']
    roles=[('worker','ai_ops.worker_runtime',[],[collector,mount('kubernetes','/var/run/ai-ops/kubernetes',True)]),
        ('event-receiver','ai_ops.alert_receiver',['--cluster-id',source['cluster_id'],'--event-source-alias',source['alias'],'--event-source-id',source['source_id']],[event]),
        ('slack-ingress','ai_ops.slack_server',['ingress','--enable-web'],[signing,tls]),
        ('slack-relay','ai_ops.slack_server',['relay','--enable-web'],[signing,bot])]
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
                  {'to':[{'ipBlock':{'cidr':c}} for c in net['slack_cidrs']], 'ports':[{'protocol':'TCP','port':443}]}]}},'networking.k8s.io/v1'))
    return {'status':'candidate_rendered_not_approved','execution_allowed':False,'live_verified':False,
        'source_revision':v['source_revision'],'preserved_c_limits_seconds':LIMITS,'manual_policy':{'rules':[],'strategy':'new_empty_active_queue_archive_old_read_only'},
        'required_live_gates':list(LIVE_GATES),'unresolved_live_inputs':sorted(UNRESOLVED),
        'external_prerequisites':['existing namespace/PVC/least-read-only service account and secrets',
            'new empty active store; stop old workers and reconcile old jobs/sends before cutover',
            'fresh source registry/UID/TLS/CI, egress IPs and additive NetworkPolicy audit',
            'existing ingress/DNS and Alertmanager direct notice preservation, no automatic route change'],
        'manifest_sha256':digest(encoded(resources)),'resources':resources}


def build(value, directory):
    result=candidate(value)
    if not result['resources']:return result
    payloads={'inputs.json':encoded(value),'candidate.json':encoded(result)}
    if any(len(x)>65536 for x in payloads.values()):raise ValueError('wf10_package_too_large')
    root=Path(directory);root.mkdir(mode=0o700,exist_ok=False)
    for name,content in payloads.items():
        fd=os.open(root/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(content)
    return {'status':result['status'],'manifest_sha256':result['manifest_sha256'],'execution_allowed':False}


def verify(directory):
    root=Path(directory)
    if root.is_symlink() or {x.name for x in root.iterdir()} != {'inputs.json','candidate.json'}:raise ValueError('wf10_package_rejected')
    expected=candidate(_json_object(read_regular(root/'inputs.json')))
    if not expected['resources'] or encoded(expected)!=read_regular(root/'candidate.json'):raise ValueError('wf10_candidate_changed')
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
