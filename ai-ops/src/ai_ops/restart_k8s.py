"""A7-3a Kubernetes adapter for the fixed lab restart (reads, evidence, PATCH, Lease).

Two separate identities: a read-only reader (Deployment/Pods/Events/HPA/Argo/logs
and Prometheus) and a writer limited to the one Deployment PATCH and the one
Lease. Every request path is on a fixed allowlist, the bearer token is re-read
per request, TLS is verified, redirects are not followed and bodies are bounded.

The port reports ``mode = 'kubernetes'``. The WF08 executor still accepts only
``local_fixture`` ports, so this adapter cannot write until a later, separately
approved WF11 decision enables live mode. Unknown or unreadable signals are
reported as unknown or blocking (fail closed), never as healthy.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import http.client
import json
from pathlib import Path
import re
import ssl
from urllib.parse import urlencode, urlsplit

from ai_ops.c_recovery import TARGET
from ai_ops.contracts import sha256_json
from ai_ops.evidence import _json_object
from ai_ops.incidents import _atomic_write, _private_directory
from ai_ops.recovery_commands import ANNOTATION, ENDPOINT, RestartSnapshot, patch_request
from ai_ops.restart_gates import REQUIRED_EVIDENCE

NAMESPACE, DEPLOYMENT, CONTAINER, LEASE = 'ai-ops-lab', 'lab-api', 'api', 'b6-scenario-lock'
PODS = f'/api/v1/namespaces/{NAMESPACE}/pods'
EVENTS = f'/api/v1/namespaces/{NAMESPACE}/events'
HPAS = f'/apis/autoscaling/v2/namespaces/{NAMESPACE}/horizontalpodautoscalers'
LEASE_PATH = f'/apis/coordination.k8s.io/v1/namespaces/{NAMESPACE}/leases/{LEASE}'
NAME = r'[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?'
READER_ROUTES = (('GET', re.escape(ENDPOINT)), ('GET', re.escape(PODS)), ('GET', re.escape(EVENTS)),
                 ('GET', re.escape(HPAS)), ('GET', re.escape(PODS) + '/' + NAME + '/log'),
                 ('GET', r'/apis/argoproj\.io/v1alpha1/namespaces/' + NAME + '/applications/' + NAME))
WRITER_ROUTES = (('PATCH', re.escape(ENDPOINT)), ('GET', re.escape(LEASE_PATH)), ('PUT', re.escape(LEASE_PATH)))
PROMETHEUS_ROUTES = (('GET', r'/api/v1/query'),)
BODY_LIMIT = 2 * 1024 * 1024
LOG_LIMIT = 262144
LEASE_SECONDS = 60
TS = re.compile(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d{1,9})?Z')


class ApiError(OSError):
    def __init__(self, status):
        super().__init__(f'kubernetes_api_status_{status}')
        self.status = status


class ApiClient:
    """Fixed-route HTTPS client. Errors never carry response bodies or tokens."""

    def __init__(self, base_url, token_file, ca_file, routes, *, now, connection_factory=None):
        url = urlsplit(base_url)
        if url.scheme != 'https' or not url.hostname or url.path not in ('', '/') or url.query:
            raise ValueError('kubernetes_base_url_rejected')
        if not Path(token_file).is_absolute() or not Path(ca_file).is_absolute():
            raise ValueError('kubernetes_credential_path_rejected')
        self._host, self._port = url.hostname, url.port or 443
        self._token_file, self._ca_file = Path(token_file), Path(ca_file)
        self._routes = [(m, re.compile(p + r'\Z')) for m, p in routes]
        self._now = now
        self._connect = connection_factory or self._https

    def _https(self, host, *, port, timeout):
        context = ssl.create_default_context(cafile=str(self._ca_file))
        return http.client.HTTPSConnection(host, port=port, timeout=timeout, context=context)

    def request(self, method, path, *, deadline, params=None, body=None,
                content_type='application/json', raw=False):
        if not any(m == method and p.fullmatch(path) for m, p in self._routes):
            raise ValueError('kubernetes_route_rejected')
        remaining = deadline - self._now()
        if remaining <= 0:
            raise TimeoutError('kubernetes_deadline')
        token = self._token_file.read_text(encoding='ascii').strip()  # re-read: tokens rotate
        if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError('kubernetes_token_rejected')
        target = path + ('?' + urlencode(sorted(params.items())) if params else '')
        headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
            headers['Content-Type'] = content_type
        conn = self._connect(self._host, port=self._port, timeout=min(3.0, remaining))
        try:
            conn.request(method, target, body=data, headers=headers)
            response = conn.getresponse()
            payload = response.read(BODY_LIMIT + 1)
        finally:
            conn.close()
        if len(payload) > BODY_LIMIT:
            raise ApiError('oversize')
        if response.status != 200:
            raise ApiError(response.status)  # 3xx included: redirects are never followed
        if raw:
            return payload
        value = _json_object(payload)
        if not isinstance(value, dict):
            raise ApiError('shape')
        return value


def parse_time(value):
    match = TS.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError('kubernetes_time_rejected')
    base = datetime.strptime(match.group(1), '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
    fraction = float('0' + match.group(2)) if match.group(2) else 0.0
    return datetime.fromtimestamp(base.timestamp() + fraction, timezone.utc)


def rfc3339(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def revision_hash(template):
    """Template fingerprint excluding only our restart annotation."""
    t = deepcopy(template)
    annotations = t.get('metadata', {}).get('annotations')
    if isinstance(annotations, dict):
        annotations.pop(ANNOTATION, None)
        if not annotations:
            t['metadata'].pop('annotations')
    return sha256_json(t)


def probe_window(container):
    """Seconds one full probe failure needs; startup counts too (pod age must cover it)."""
    if not isinstance(container.get('readinessProbe'), dict):
        raise ValueError('readiness_probe_missing')
    windows = []
    for key in ('readinessProbe', 'startupProbe'):
        probe = container.get(key)
        if isinstance(probe, dict):
            windows.append(int(probe.get('initialDelaySeconds', 0))
                           + int(probe.get('failureThreshold', 3)) * int(probe.get('periodSeconds', 10)))
    return max(windows)


def derive_snapshot(deployment, pods, hpas, gitops_auto_sync, events, prior_ready, dependencies, now):
    """Pure: verified API objects + signal verdicts -> RestartSnapshot (raises if inconsistent)."""
    meta, spec, status = deployment['metadata'], deployment['spec'], deployment.get('status', {})
    template = spec['template']
    containers = template['spec']['containers']
    if (meta.get('name') != DEPLOYMENT or meta.get('namespace') != NAMESPACE or len(containers) != 1
            or containers[0].get('name') != CONTAINER or spec.get('replicas') != 1):
        raise ValueError('restart_deployment_shape_rejected')
    live = [p for p in pods if not p['metadata'].get('deletionTimestamp')]
    if len(live) != 1:
        raise ValueError('restart_pod_count_rejected')
    pod = live[0]
    statuses = [c for c in pod.get('status', {}).get('containerStatuses', []) if c.get('name') == CONTAINER]
    if len(statuses) != 1 or not pod.get('status', {}).get('startTime'):
        raise ValueError('restart_pod_status_rejected')
    cs = statuses[0]
    ready = [c for c in pod['status'].get('conditions', []) if c.get('type') == 'Ready']
    failing_since = (parse_time(ready[0]['lastTransitionTime'])
                     if ready and ready[0].get('status') == 'False' and ready[0].get('lastTransitionTime') else None)
    annotations = template.get('metadata', {}).get('annotations')
    rollout = (status.get('observedGeneration') != meta.get('generation')
               or status.get('updatedReplicas', 0) != 1 or status.get('replicas', 0) != 1)
    autoscaling = any(h.get('spec', {}).get('scaleTargetRef', {}).get('kind') == 'Deployment'
                      and h['spec']['scaleTargetRef'].get('name') == DEPLOYMENT for h in hpas)
    image_id = cs.get('imageID')
    return RestartSnapshot(
        target=TARGET.model_copy(deep=True), deployment_uid=meta['uid'], resource_version=meta['resourceVersion'],
        revision_hash=revision_hash(template), image=containers[0]['image'],
        pod_uid_hash=sha256_json(pod['metadata']['uid']), observed_at=now, generation=meta['generation'],
        observed_generation=status.get('observedGeneration', 0), ready_replicas=status.get('readyReplicas', 0),
        strategy=spec.get('strategy', {}).get('type', 'RollingUpdate'),
        container_running='running' in cs.get('state', {}), restart_count=cs.get('restartCount', 0),
        pod_started_at=parse_time(pod['status']['startTime']), readiness_failing_since=failing_since,
        readiness_window_seconds=probe_window(containers[0]), prior_ready=prior_ready, dependencies=dependencies,
        image_id_hash=sha256_json(image_id) if isinstance(image_id, str) and image_id else None,
        scheduling_signals=sorted({'failed_scheduling' for e in events if e.get('reason') == 'FailedScheduling'}),
        rollout_in_progress=rollout, autoscaling=autoscaling, gitops_auto_sync=gitops_auto_sync,
        annotations_exist=isinstance(annotations, dict), restart_marker=(annotations or {}).get(ANNOTATION))


class PrometheusSignals:
    """Configured PromQL only; nothing configured or nothing returned means unknown.

    ``prior_ready`` may contain the literal placeholder ``$TEMPLATE_HASH`` (plain
    replacement, not str.format: PromQL label selectors use braces);
    ``dependencies`` is a list of expressions that must each return 1 (0 = fail).
    """

    def __init__(self, client, *, prior_ready=None, dependencies=()):
        self.client, self.prior_query, self.dependency_queries = client, prior_ready, tuple(dependencies)

    def _values(self, expression, deadline):
        body = self.client.request('GET', '/api/v1/query', params={'query': expression}, deadline=deadline)
        data = body.get('data', {}) if body.get('status') == 'success' else {}
        if data.get('resultType') != 'vector':
            raise ValueError('prometheus_shape')
        return [float(item['value'][1]) for item in data.get('result', [])]

    def prior_ready(self, template_hash, deadline):
        if not self.prior_query or not re.fullmatch(r'[a-z0-9]{1,16}', template_hash or ''):
            return 'unknown'
        try:
            values = self._values(self.prior_query.replace('$TEMPLATE_HASH', template_hash), deadline)
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return 'unknown'
        if not values:
            return 'unknown'
        return 'observed' if max(values) >= 1 else 'absent'

    def dependencies(self, deadline):
        if not self.dependency_queries:
            return 'unknown'
        verdicts = []
        for expression in self.dependency_queries:
            try:
                values = self._values(expression, deadline)
            except (OSError, ValueError, KeyError, TypeError, IndexError):
                return 'unknown'
            if not values:
                return 'unknown'
            verdicts.append('fail' if min(values) < 1 else 'pass')
        return 'fail' if 'fail' in verdicts else 'pass'


class KubernetesRestartPort:
    mode = 'kubernetes'  # not 'local_fixture': the WF08 executor refuses this port

    def __init__(self, *, executor_id, reader, writer, signals, evidence_dir, now, argo_application):
        if not re.fullmatch(r'local-restart-[a-z0-9-]{1,40}', executor_id or ''):
            raise ValueError('restart_executor_id_rejected')
        namespace, name = argo_application
        if not re.fullmatch(NAME, namespace) or not re.fullmatch(NAME, name):
            raise ValueError('argo_application_rejected')
        self.identity = {'executor_id': executor_id, 'namespace': NAMESPACE,
                         'resource': 'deployments/lab-api', 'verbs': ['get', 'patch']}
        self.reader, self.writer, self.signals = reader, writer, signals
        self._evidence_dir, self._now = Path(evidence_dir), now
        self._argo = f'/apis/argoproj.io/v1alpha1/namespaces/{namespace}/applications/{name}'

    def _objects(self, deadline):
        deployment = self.reader.request('GET', ENDPOINT, deadline=deadline)
        selector = deployment['spec']['selector']['matchLabels']
        if not selector or not all(isinstance(v, str) for v in selector.values()):
            raise ValueError('restart_selector_rejected')
        pods = self.reader.request('GET', PODS, deadline=deadline, params={
            'labelSelector': ','.join(f'{k}={v}' for k, v in sorted(selector.items()))})['items']
        return deployment, pods

    def _gitops(self, deadline):
        try:
            app = self.reader.request('GET', self._argo, deadline=deadline)
        except (OSError, ValueError):
            return True  # cannot prove auto-sync is off: treat as on (blocks)
        return app.get('spec', {}).get('syncPolicy', {}).get('automated') is not None

    def read(self, *, deadline):
        deployment, pods = self._objects(deadline)
        hpas = self.reader.request('GET', HPAS, deadline=deadline)['items']
        live = [p for p in pods if not p['metadata'].get('deletionTimestamp')]
        events = []
        template_hash = None
        if len(live) == 1:
            template_hash = live[0]['metadata'].get('labels', {}).get('pod-template-hash')
            try:
                events = self.reader.request('GET', EVENTS, deadline=deadline, params={
                    'fieldSelector': 'involvedObject.uid=' + live[0]['metadata']['uid']})['items']
            except (OSError, ValueError):
                events = []  # display-only signal
        now = datetime.fromtimestamp(self._now(), timezone.utc)
        return derive_snapshot(deployment, pods, hpas, self._gitops(deadline), events,
                               self.signals.prior_ready(template_hash, deadline),
                               self.signals.dependencies(deadline), now)

    def _logs(self, pod, deadline, previous):
        params = {'container': CONTAINER, 'limitBytes': str(LOG_LIMIT), 'tailLines': '500'}
        if previous:
            params['previous'] = 'true'
        try:
            data = self.reader.request('GET', f'{PODS}/{pod}/log', deadline=deadline, params=params, raw=True)
        except ApiError as error:
            if previous and error.status == 400:
                return {'absent': True}  # no previous container: recorded, not fabricated
            raise
        return {'text': data.decode('utf-8', 'replace')}

    def capture_evidence(self, *, deadline):
        started = self._now()
        deployment, pods = self._objects(deadline)
        live = [p for p in pods if not p['metadata'].get('deletionTimestamp')]
        if len(live) != 1:
            raise ValueError('restart_pod_count_rejected')
        pod = live[0]
        name = pod['metadata']['name']
        if not re.fullmatch(NAME, name):
            raise ValueError('restart_pod_name_rejected')
        strip = lambda obj: {k: v for k, v in obj.items() if k != 'metadata'} | {
            'metadata': {k: v for k, v in obj['metadata'].items() if k != 'managedFields'}}
        events = self.reader.request('GET', EVENTS, deadline=deadline, params={
            'fieldSelector': 'involvedObject.uid=' + pod['metadata']['uid']})['items']
        template_hash = pod['metadata'].get('labels', {}).get('pod-template-hash')
        bundle = {
            'describe': {'deployment': strip(deployment), 'pod': strip(pod)},
            'status': {'deployment': deployment.get('status', {}), 'pod': pod.get('status', {})},
            'events': events,
            'logs_current': self._logs(name, deadline, False),
            'logs_previous': self._logs(name, deadline, True),
            'metrics': {'prior_ready': self.signals.prior_ready(template_hash, deadline),
                        'dependencies': self.signals.dependencies(deadline)},
        }
        data = (json.dumps(bundle, ensure_ascii=False, sort_keys=True) + '\n').encode()
        digest = hashlib.sha256(data).hexdigest()  # re-checkable from the stored file
        _private_directory(self._evidence_dir)
        _atomic_write(self._evidence_dir / f'{digest}.json', data)  # logs stay private, never in ledgers
        captured = self._now()
        if not started <= captured <= deadline:
            raise TimeoutError('restart_evidence_deadline')
        return {'bundle_sha256': digest, 'items': list(REQUIRED_EVIDENCE), 'captured_at': captured}

    def patch(self, request, *, deadline):
        body = request.get('body') if isinstance(request, dict) else None
        ops = body if isinstance(body, list) else []
        uid = next((o.get('value') for o in ops if o.get('path') == '/metadata/uid'), None)
        rv = next((o.get('value') for o in ops if o.get('path') == '/metadata/resourceVersion'), None)
        marker = ops[-1].get('value') if ops else None
        if (not isinstance(uid, str) or not isinstance(rv, str) or not isinstance(marker, str)
                or request != patch_request(uid, rv, marker, len(ops) == 3)):
            raise ValueError('restart_patch_request_rejected')  # only the sealed JSON Patch shape
        try:
            self.writer.request('PATCH', ENDPOINT, deadline=deadline, body=ops,
                                content_type='application/json-patch+json')
        except ApiError as error:
            if error.status in (409, 422):
                raise ValueError('json_patch_test_conflict') from None  # definite non-apply
            raise OSError('restart_patch_outcome_unknown') from None
        return {'accepted': True}

    def _lease(self, deadline):
        lease = self.writer.request('GET', LEASE_PATH, deadline=deadline)
        if lease.get('metadata', {}).get('name') != LEASE or lease['metadata'].get('namespace') != NAMESPACE:
            raise ValueError('restart_lease_rejected')
        return lease

    def _view(self, lease):
        spec = lease.get('spec', {})
        renew = spec.get('renewTime')
        duration = spec.get('leaseDurationSeconds')
        until = parse_time(renew).timestamp() + duration if renew and type(duration) is int else 0
        return {'name': LEASE, 'namespace': NAMESPACE, 'uid': lease['metadata']['uid'],
                'holder': spec.get('holderIdentity'), 'verified_until': until}

    def acquire(self, *, owner, deadline):
        lease = self._lease(deadline)
        if lease.get('spec', {}).get('holderIdentity'):
            raise ValueError('restart_lease_held')  # never take over, even if apparently expired
        now = rfc3339(self._now())
        updated = deepcopy(lease)
        updated['spec'] = {**lease.get('spec', {}), 'holderIdentity': owner, 'leaseDurationSeconds': LEASE_SECONDS,
                           'acquireTime': now, 'renewTime': now}
        try:
            result = self.writer.request('PUT', LEASE_PATH, deadline=deadline, body=updated)
        except ApiError as error:
            if error.status == 409:
                raise ValueError('restart_lease_conflict') from None
            raise
        return self._view(result)

    def inspect_lease(self, held, *, deadline):
        return self._view(self._lease(deadline))

    def release(self, held, *, deadline):
        lease = self._lease(deadline)
        view = self._view(lease)
        if view['uid'] != held.get('uid') or view['holder'] != held.get('holder'):
            return False  # not ours anymore: never clear another holder
        updated = deepcopy(lease)
        updated['spec'] = {k: v for k, v in lease.get('spec', {}).items()
                           if k not in ('holderIdentity', 'acquireTime', 'renewTime')}
        try:
            self.writer.request('PUT', LEASE_PATH, deadline=deadline, body=updated)
        except (ApiError, ValueError):
            return False
        return True
