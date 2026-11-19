"""A7-4 bounded post-restart observation feeding WF09 effect verification.

WF09 judges a restart only from samples taken *during* an observation window
(at most ``maximum_gap_seconds`` apart). Prometheus history cannot rebuild the
HTTP response contract or alert-rule evaluation state, and range-query steps are
lookback-filled rather than observed, so this module observes directly:

* one bounded run per applied restart, holding no lock (the WF08 executor has
  already released its Lease after inspecting the write);
* the window starts only after the container's full startup/readiness allowance
  (``probe_window``), so the expected not-ready gap of a single-replica Recreate
  restart is never judged, while a Pod still not Ready after it is a failure;
* each tick reads Kubernetes runtime state, ``/readyz`` and ``/items`` through
  the API server Pod proxy (fixed GET routes, no new network path), configured
  PromQL via the C-04 freshness check and configured alert rules;
* any failed read marks only its series ``collection_failed`` (never healthy);
* the batch is sealed into a private 0600 file named by its sha256; WF09 reads
  only those exact bytes. Raw responses are hashed, never stored.

Ports report non-fixture modes, so the WF09 coordinator still refuses them
until a separately approved live decision.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Annotated, Literal
from urllib.parse import parse_qsl

from pydantic import Field, StrictInt, StrictStr

from ai_ops.c_verification import (AlertReading, AlertRequirement, HTTPReading, MetricReading, MetricRequirement,
                                   Name)
from ai_ops.c_verification_collect import definition_hash, metric_value, valid_body
from ai_ops.contracts import StrictModel, sha256_json
from ai_ops.evidence import _json_object
from ai_ops.incidents import _atomic_write, _private_directory
from ai_ops.recovery_commands import ANNOTATION, ENDPOINT
from ai_ops.recovery_effects import CleanupItem, EffectBatch, EffectPolicy, ExecutionReceipt, RuntimeEffect
from ai_ops.restart_k8s import (CONTAINER, LEASE, LEASE_PATH, NAME, NAMESPACE, PODS, ApiError, parse_time,
                                probe_window, revision_hash)

HTTP_PORT = 8000
PROXY = re.escape(PODS) + '/' + NAME + re.escape(f':{HTTP_PORT}/proxy/') + '(readyz|items)'
EFFECT_READER_ROUTES = (('GET', re.escape(ENDPOINT)), ('GET', re.escape(PODS)), ('GET', PROXY))
EFFECT_PROMETHEUS_ROUTES = (('GET', r'/api/v1/query'), ('GET', r'/api/v1/rules'))
LEASE_ROUTES = (('GET', re.escape(LEASE_PATH)), ('PUT', re.escape(LEASE_PATH)))
SEAL_LIMIT = 8 * 1024 * 1024
SHA = re.compile(r'[a-f0-9]{64}')
DIGEST = re.compile(r'sha256:[a-f0-9]{64}')


class AlertRuleDefinition(StrictModel):
    """Operator-pinned alert rule; the live rule's hash is compared by WF09."""
    name: Name
    query: Annotated[StrictStr, Field(min_length=1, max_length=4096)]
    labels: dict[StrictStr, StrictStr] = Field(max_length=32)
    duration: Annotated[float, Field(ge=0, le=86400, allow_inf_nan=False)]
    keepFiringFor: Annotated[float, Field(ge=0, le=86400, allow_inf_nan=False)] = 0
    group: Annotated[StrictStr, Field(min_length=1, max_length=256)]
    file: Annotated[StrictStr, Field(min_length=1, max_length=1024)]


class MetricProbe(StrictModel):
    name: Name
    query: Annotated[StrictStr, Field(min_length=1, max_length=4096)]
    labels: dict[StrictStr, StrictStr] = Field(max_length=16)
    comparison: Literal['lte', 'gte']
    threshold: Annotated[float, Field(allow_inf_nan=False)]

    def requirement(self):
        return MetricRequirement(name=self.name, definition_hash=definition_hash(self.query),
                                 comparison=self.comparison, threshold=self.threshold)


class AlertProbe(StrictModel):
    definition: AlertRuleDefinition
    scope_labels: dict[StrictStr, StrictStr] = Field(min_length=1, max_length=8)

    def requirement(self):
        return AlertRequirement(name=self.definition.name,
                                definition_hash=definition_hash(self.definition.model_dump(mode='json')))


class ObservationConfig(StrictModel):
    """Configured queries only: nothing is guessed from metric names."""
    interval_seconds: Annotated[StrictInt, Field(ge=5, le=20)]
    source_max_age_seconds: Annotated[StrictInt, Field(ge=15, le=90)]
    metrics: list[MetricProbe] = Field(min_length=1, max_length=12)
    alerts: list[AlertProbe] = Field(min_length=1, max_length=12)


def body(value):
    """Plain JSON form of a model, or the value itself (trusted internal dicts)."""
    return value.model_dump(mode='json') if hasattr(value, 'model_dump') else value


def effect_window(applied_at, startup_seconds, span_seconds):
    """Window starting after the full startup allowance (expected restart gap excluded)."""
    start = applied_at + timedelta(seconds=startup_seconds)
    return start, start + timedelta(seconds=span_seconds)


class _Stop(Exception):
    """Ends the run: identity changed or cancelled (every series becomes unknown)."""


def _reject_constant(_):
    raise ValueError('non_finite_json')


class _PromShim:
    """Adapts the fixed-route client to the C-04 ``reader.http(port, path)`` contract."""

    def __init__(self, client, deadline):
        self.client, self.deadline, self.seen = client, deadline, []

    def http(self, port, path):
        route, _, query = path.partition('?')
        try:
            data = self.client.request('GET', route, deadline=self.deadline,
                                       params=dict(parse_qsl(query, keep_blank_values=True)), raw=True)
        except ApiError as error:
            return error.status, None, None
        self.seen.append(hashlib.sha256(data).hexdigest())
        return 200, _json_object(data), None


class RestartEffectObserver:
    mode = 'kubernetes'  # not 'local_fixture': the WF09 coordinator refuses live readers

    def __init__(self, *, collector, reader, prometheus, config, now, sleep):
        if not re.fullmatch(r'local-restart-[a-z0-9-]{1,40}', collector or ''):
            raise ValueError('effect_collector_rejected')
        self.collector, self.reader, self.prometheus = collector, reader, prometheus
        self.config = ObservationConfig.model_validate(body(config))
        self._now, self._sleep = now, sleep

    def _at(self):
        return datetime.fromtimestamp(self._now(), timezone.utc)

    def _raw(self, client, path, deadline, params=None):
        data = client.request('GET', path, deadline=deadline, params=params, raw=True)
        return hashlib.sha256(data).hexdigest(), _json_object(data)

    def _runtime(self, receipt, deadline):
        dh, deployment = self._raw(self.reader, ENDPOINT, deadline)
        meta, spec, status = deployment['metadata'], deployment['spec'], deployment.get('status', {})
        if meta.get('uid') != receipt.deployment_uid:
            raise _Stop('observation_identity_changed')
        selector = spec['selector']['matchLabels']
        if not selector or not all(isinstance(v, str) for v in selector.values()):
            raise ValueError('effect_selector_rejected')
        ph, listing = self._raw(self.reader, PODS, deadline, {
            'labelSelector': ','.join(f'{k}={v}' for k, v in sorted(selector.items()))})
        pods, template = listing['items'], spec['template']
        containers = template['spec']['containers']
        if len(containers) != 1 or containers[0].get('name') != CONTAINER:
            raise ValueError('effect_container_rejected')
        image = containers[0]['image']
        digest = image.split('@', 1)[1] if '@' in image else None
        live = [p for p in pods if not p['metadata'].get('deletionTimestamp')]
        ready = []
        for pod in live:
            statuses = [c for c in pod.get('status', {}).get('containerStatuses', []) if c.get('name') == CONTAINER]
            running = (statuses[0].get('imageID') or '') if len(statuses) == 1 else ''
            if digest is not None and '@' in running and running.split('@', 1)[1] != digest:
                digest = running.split('@', 1)[1]  # a different running image decides (-> mismatch)
            conditions = pod.get('status', {}).get('conditions', [])
            if (len(statuses) == 1 and statuses[0].get('ready') is True
                    and any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in conditions)):
                ready.append(pod['metadata']['name'])
        if digest is None or not DIGEST.fullmatch(digest):
            raise ValueError('effect_image_not_pinned')
        marker = (template.get('metadata', {}).get('annotations') or {}).get(ANNOTATION)
        reading = RuntimeEffect(
            revision=revision_hash(template), application_uid=None, digest=digest, generation=meta['generation'],
            observed_generation=status.get('observedGeneration', 0), replicas=status.get('replicas', 0),
            ready_replicas=status.get('readyReplicas', 0),
            # Terminating Pods count: an old Pod still present is not "replaced".
            pod_uid_hashes=sorted(sha256_json(p['metadata']['uid']) for p in pods), ready_pods=len(ready),
            marker=marker if isinstance(marker, str) and SHA.fullmatch(marker) else None, argo_synced_healthy=None)
        return reading, sha256_json([dh, ph]), ready

    def _http(self, pod, path, deadline):
        if not re.fullmatch(NAME, pod):
            raise ValueError('effect_pod_name_rejected')
        try:
            data = self.reader.request('GET', f'{PODS}/{pod}:{HTTP_PORT}/proxy{path}', deadline=deadline, raw=True)
        except ApiError as error:
            # Proxy errors cannot be told apart from app errors: unknown, never a pass.
            return HTTPReading(status_code=None, response_contract_valid=None), sha256_json({'status': str(error.status)})
        try:
            valid = valid_body(path, json.loads(data, parse_constant=_reject_constant))
        except (ValueError, UnicodeDecodeError, RecursionError):
            valid = False
        return HTTPReading(status_code=200, response_contract_valid=valid), hashlib.sha256(data).hexdigest()

    def _metric(self, probe, receipt, deadline):
        shim, at = _PromShim(self.prometheus, deadline), self._at()
        value = metric_value(shim, None, probe.query, probe.labels, at, self.config.source_max_age_seconds,
                             receipt.applied_at)
        return MetricReading(value=value), sha256_json(shim.seen)

    def _alert(self, probe, deadline):
        name = probe.definition.name
        h, data = self._raw(self.prometheus, '/api/v1/rules', deadline, {'type': 'alert', 'rule_name[]': name})
        if data.get('status') != 'success' or data.get('warnings') or data.get('infos'):
            raise ValueError('rules_response_unavailable')
        rules = data['data']
        if rules.get('groupNextToken'):
            raise ValueError('rules_incomplete')
        matches = [(g, r) for g in rules.get('groups', []) for r in g.get('rules', [])
                   if r.get('name') == name and r.get('type') == 'alerting']
        if len(matches) != 1:
            raise ValueError('alert_rule_missing_or_ambiguous')
        group, rule = matches[0]
        live = AlertRuleDefinition(name=rule['name'], query=rule['query'], labels=rule.get('labels', {}),
                                   duration=rule['duration'], keepFiringFor=rule.get('keepFiringFor', 0),
                                   group=group['name'], file=group['file'])
        evaluation = 'error' if rule.get('lastError') else {'ok': 'ok', 'err': 'error'}.get(rule.get('health'), 'no_data')
        active = 0
        for alert in rule.get('alerts', []):
            labels = alert.get('labels', {})
            if not all(k in labels for k in probe.scope_labels):
                raise ValueError('alert_scope_unavailable')
            if all(labels[k] == v for k, v in probe.scope_labels.items()):
                if alert.get('state') not in ('pending', 'firing'):
                    raise ValueError('alert_state_unavailable')
                active += 1
        reading = AlertReading(rule_present=True, evaluation=evaluation,
                               evaluated_at=parse_time(rule['lastEvaluation']), active_count=active)
        return reading, definition_hash(live.model_dump(mode='json')), h

    def observe(self, receipt, policy, *, run_id, cancelled=lambda: False):
        receipt = ExecutionReceipt.model_validate(body(receipt))
        policy = EffectPolicy.model_validate(body(policy))
        cfg = self.config
        if (receipt.method != 'command' or not receipt.applied or receipt.applied_at is None or receipt.cancelled
                or policy.window_start is None):
            raise ValueError('effect_observation_receipt_rejected')
        if not re.fullmatch(r'effect-[a-f0-9]{32}', run_id or ''):
            raise ValueError('effect_run_id_rejected')
        if ([x.model_dump() for x in policy.metrics] != [m.requirement().model_dump() for m in cfg.metrics]
                or [x.model_dump() for x in policy.alerts] != [a.requirement().model_dump() for a in cfg.alerts]):
            raise ValueError('effect_observation_config_mismatch')
        start, end = policy.window_start.timestamp(), policy.window_end.timestamp()
        if cfg.interval_seconds + policy.read_seconds > policy.maximum_gap_seconds:
            raise ValueError('effect_interval_exceeds_gap')  # worst gap = interval + one read budget
        if end - start < policy.minimum_span_seconds + cfg.interval_seconds + 2 * policy.read_seconds:
            raise ValueError('effect_window_too_short_for_span')  # sampled span must still reach the minimum
        if (end - start) // cfg.interval_seconds + 2 > 100:
            raise ValueError('effect_samples_exceed_limit')
        if end + policy.maximum_report_delay_seconds > receipt.retained_until.timestamp():
            raise ValueError('effect_window_after_retention')
        deployment = self.reader.request('GET', ENDPOINT, deadline=self._now() + policy.read_seconds)
        if deployment['metadata'].get('uid') != receipt.deployment_uid:
            raise ValueError('effect_identity_changed')
        allowance = probe_window(deployment['spec']['template']['spec']['containers'][0])
        if policy.window_start < receipt.applied_at + timedelta(seconds=allowance):
            raise ValueError('effect_window_before_startup')
        if self._now() > start:
            raise ValueError('effect_window_already_started')

        source = lambda kind, name: sha256_json({'run_id': run_id, 'collector': self.collector,
                                                 'kind': kind, 'name': name})
        series = {}
        for kind, name, definition in [('runtime', 'lab-api', None), ('http', '/readyz', None),
                                       ('http', '/items', None),
                                       *[('metric', m.name, m.requirement().definition_hash) for m in cfg.metrics],
                                       *[('alert', a.definition.name, None) for a in cfg.alerts]]:
            series[kind, name] = dict(kind=kind, name=name, target=body(receipt.target),
                                      deployment_uid=receipt.deployment_uid, execution_hash=receipt.digest_key(),
                                      definition_hash=definition, source_hash=source(kind, name),
                                      status='available', samples=[])
        reasons = set()

        def fail(key, reason):
            reasons.add(reason)
            series[key].update(status='collection_failed', samples=[])

        def read(key, deadline, call):
            if series[key]['status'] == 'collection_failed':
                return None
            try:
                reading, digest, extra = call()
                at = self._at()
                if at.timestamp() > min(deadline, end):
                    raise TimeoutError('effect_read_deadline')
            except _Stop:
                raise
            except Exception:  # never persist response bodies or exception text
                fail(key, f'{key[0]}_collection_failed')
                return None
            series[key]['samples'].append(dict(at=at.isoformat(), reading=body(reading), source_sha256=digest))
            return extra

        tick = start
        try:
            # The last tick still completes inside the window: end gap <= interval + read budget.
            while tick + policy.read_seconds <= end:
                self._wait(tick, cancelled)
                if cancelled():
                    raise _Stop('cancelled')
                deadline = min(self._now() + policy.read_seconds, end)
                ready = read(('runtime', 'lab-api'), deadline, lambda: self._runtime(receipt, deadline))
                for path in ('/readyz', '/items'):
                    if ready is not None and len(ready) == 1:
                        read(('http', path), deadline, lambda p=path: (*self._http(ready[0], p, deadline), None))
                    elif series['http', path]['status'] != 'collection_failed':
                        fail(('http', path), 'http_target_not_single_ready')
                for m in cfg.metrics:
                    read(('metric', m.name), deadline, lambda m=m: (*self._metric(m, receipt, deadline), None))
                for a in cfg.alerts:
                    key = ('alert', a.definition.name)

                    def alert(a=a, key=key):
                        reading, live_hash, digest = self._alert(a, deadline)
                        if series[key]['definition_hash'] not in (None, live_hash):
                            raise ValueError('alert_definition_changed')
                        series[key]['definition_hash'] = live_hash
                        return reading, digest, None
                    read(key, deadline, alert)
                tick = max(tick + cfg.interval_seconds, self._now())
        except _Stop as stop:
            for key in series:
                fail(key, stop.args[0])
        for item in series.values():
            if item['status'] == 'available' and not item['samples']:
                item['status'] = 'missing'
        batch = EffectBatch.model_validate(dict(provenance='captured', run_id=run_id, collector=self.collector,
                                                series=list(series.values())))
        return {'batch': body(batch), 'reasons': sorted(reasons), 'execution_allowed': False}

    def _wait(self, until, cancelled):
        while self._now() < until:
            if cancelled():
                return
            self._sleep(min(1.0, until - self._now()))


class EffectCaptureStore:
    """Seals captured batches as private files named by the sha256 of their bytes."""

    def __init__(self, directory):
        self.directory = Path(directory)

    def seal(self, batch):
        batch = EffectBatch.model_validate(body(batch))
        if batch.provenance != 'captured':
            raise ValueError('effect_seal_requires_capture')
        data = (json.dumps(body(batch), ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n').encode()
        if len(data) > SEAL_LIMIT:
            raise ValueError('effect_seal_oversize')
        digest = hashlib.sha256(data).hexdigest()
        _private_directory(self.directory)
        _atomic_write(self.directory / f'{digest}.json', data)
        return {'sealed_sha256': digest, 'run_id': batch.run_id, 'series': len(batch.series),
                'samples': sum(len(s.samples) for s in batch.series)}


class SealedEffectReader:
    """WF09 reader over one sealed capture (the ledger keeps only its hash)."""
    mode = 'sealed_capture'  # not 'local_fixture': refused until a live decision

    def __init__(self, directory, sealed_sha256, *, now):
        if not SHA.fullmatch(sealed_sha256 or ''):
            raise ValueError('effect_seal_hash_rejected')
        self.path, self.expected, self._now = Path(directory) / f'{sealed_sha256}.json', sealed_sha256, now

    def read(self, receipt, policy, *, deadline):
        if self._now() >= deadline:
            raise TimeoutError('effect_read_deadline')
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077
                    or info.st_size > SEAL_LIMIT):
                raise ValueError('effect_seal_file_rejected')
            data = os.read(fd, SEAL_LIMIT + 1)
        finally:
            os.close(fd)
        if hashlib.sha256(data).hexdigest() != self.expected:
            raise ValueError('effect_seal_mismatch')
        batch = EffectBatch.model_validate(_json_object(data))
        execution = ExecutionReceipt.model_validate(body(receipt)).digest_key()
        if batch.provenance != 'captured' or any(s.execution_hash != execution for s in batch.series):
            raise ValueError('effect_seal_binding_rejected')
        if self._now() >= deadline:
            raise TimeoutError('effect_read_deadline')
        return batch  # model, as WF09 readers return


def restart_lease_item(lease):
    """Cleanup item for the WF08 restart Lease record (identity + holder, hashed)."""
    if (not isinstance(lease, dict) or lease.get('name') != LEASE or lease.get('namespace') != NAMESPACE
            or not isinstance(lease.get('uid'), str) or not lease['uid'] or not isinstance(lease.get('holder'), str)
            or not lease['holder']):
        raise ValueError('restart_lease_record_rejected')
    return CleanupItem(kind='lease', item_hash=sha256_json({'name': LEASE, 'namespace': NAMESPACE, 'uid': lease['uid']}),
                       owner_hash=sha256_json(lease['holder']), action='release_lease')


class LeaseCleanupPort:
    """Confirms the restart Lease is released; clears it only if still ours."""
    mode = 'kubernetes'  # not 'local_fixture': the WF09 coordinator refuses live cleanup

    def __init__(self, writer):
        self.writer = writer

    def _current(self, item, deadline):
        if item.kind != 'lease':
            raise ValueError('cleanup_kind_unsupported')  # unknown -> manual handoff
        try:
            lease = self.writer.request('GET', LEASE_PATH, deadline=deadline)
        except ApiError as error:
            if error.status == 404:
                return None
            raise
        meta = lease.get('metadata', {})
        if meta.get('name') != LEASE or meta.get('namespace') != NAMESPACE:
            raise ValueError('restart_lease_rejected')
        if sha256_json({'name': LEASE, 'namespace': NAMESPACE, 'uid': meta.get('uid')}) != item.item_hash:
            return None  # recreated object: the item we held no longer exists
        return lease

    def inspect(self, item, *, deadline):
        lease = self._current(item, deadline)
        holder = (lease or {}).get('spec', {}).get('holderIdentity')
        if not holder:
            return {'present': False}
        return {'present': True, 'owner_hash': sha256_json(holder)}

    def dispose(self, item, *, deadline):
        lease = self._current(item, deadline)
        holder = (lease or {}).get('spec', {}).get('holderIdentity')
        if not holder:
            return
        if sha256_json(holder) != item.owner_hash:
            raise ValueError('restart_lease_not_owned')  # never clear another holder
        cleared = {**lease, 'spec': {k: v for k, v in lease.get('spec', {}).items()
                                     if k not in ('holderIdentity', 'acquireTime', 'renewTime')}}
        try:
            self.writer.request('PUT', LEASE_PATH, deadline=deadline, body=cleared)  # resourceVersion guards races
        except ApiError as error:
            if error.status == 409:
                raise ValueError('restart_lease_cleanup_conflict') from None
            raise OSError('restart_lease_cleanup_unknown') from None
