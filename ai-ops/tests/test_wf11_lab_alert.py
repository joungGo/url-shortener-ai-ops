"""WF11 lab alert (llm-council 2026-11-25): rendered PrometheusRule and its label contract.

The rule raises one event for the registered lab target when the Deployment wants
replicas but has none available (an image-pull failure leaves zero Pods, so the
request-metric alerts cannot fire). Offline: real renderer, real EventStore.
"""
from copy import deepcopy
import json
import unittest

import test_event_store as events
import test_wf10_recovery_render as render
from ai_ops.alerts import AlertInbox, RejectedAlert
from ai_ops.k8s_schema import SchemaError
from ai_ops.wf10_deployment import (LAB_ALERT, candidate, check_lab_rule,
                                    lab_alert_rule)

TARGET_KEYS = ('cluster_id', 'namespace', 'workload', 'service', 'environment')


def fired(rule_labels, workload, namespace, **changes):
    """Alertmanager webhook item as Prometheus emits it for this rule: rule labels
    plus the series labels kept by `max by (namespace, deployment)` and alertname."""
    item = {'labels': {**rule_labels, 'alertname': LAB_ALERT, 'namespace': namespace, 'deployment': workload},
            'annotations': {'summary': 'static'}, 'status': 'firing', 'fingerprint': '0123456789abcdef',
            'startsAt': '1970-01-01T00:16:40Z', 'generatorURL': 'https://prometheus.invalid/'}
    item.update(changes)
    return item


class RenderedRuleTest(unittest.TestCase):
    """Composes the recovery render fixture (its own tests are not re-run here)."""
    def setUp(self):
        self.r = render.RecoveryRenderTest()
        self.r.setUp()
        self.addCleanup(self.r.doCleanups)
        self.v = self.r.v

    def rule(self, value=None):
        result = candidate(value or self.v)
        return next(r for r in result['resources'] if r['kind'] == 'PrometheusRule')

    def test_label_contract_comes_from_the_single_binding(self):
        rule = self.rule()
        binding = self.v['config']['bindings.json']['bindings'][0]['target']
        # Literal values (not the module constants): the operator selects rules by
        # release=kube-prometheus-stack (same label as the platform url-shortener rule).
        self.assertEqual(rule['metadata'], {'name': 'wf10-fixture-lab-alerts', 'namespace': self.v['namespace'],
                                            'labels': {'release': 'kube-prometheus-stack',
                                                       'app.kubernetes.io/part-of': 'wf10'}})
        [entry] = rule['spec']['groups'][0]['rules']
        self.assertEqual((entry['alert'], entry['for']), ('LabDeploymentUnavailable', '2m'))
        self.assertEqual(entry['labels'], {'severity': 'warning', 'ai_ops': 'enabled',
                                           **{k: binding[k] for k in TARGET_KEYS}})
        selector = f'namespace="{binding["namespace"]}",deployment="{binding["workload"]}"'
        self.assertEqual(entry['expr'],
                         f'(max by (namespace, deployment) (kube_deployment_spec_replicas{{{selector}}}) > 0)'
                         f' and on (namespace, deployment) '
                         f'(max by (namespace, deployment) (kube_deployment_status_replicas_available{{{selector}}}) == 0)')
        self.assertNotIn('{{', json.dumps(entry['annotations']))
        check_lab_rule(rule)

    def test_absent_recovery_renders_no_rule(self):
        from test_wf10_deployment import fixture
        self.assertFalse(any(r['kind'] == 'PrometheusRule' for r in candidate(fixture())['resources']))

    def test_shape_check_rejects_variants(self):
        rule = self.rule()
        cases = {
            'extra_top': lambda r: r.update(status={}),
            'extra_rule_field': lambda r: r['spec']['groups'][0]['rules'][0].update(keep_firing_for='1m'),
            'two_rules': lambda r: r['spec']['groups'][0]['rules'].append(deepcopy(r['spec']['groups'][0]['rules'][0])),
            'templated_annotation': lambda r: r['spec']['groups'][0]['rules'][0]['annotations'].update(
                summary='{{ $labels.pod }}'),
            'templated_label': lambda r: r['spec']['groups'][0]['rules'][0]['labels'].update(service='{{ $value }}'),
            'bad_duration': lambda r: r['spec']['groups'][0]['rules'][0].update({'for': 'two minutes'}),
            'non_string_label': lambda r: r['spec']['groups'][0]['rules'][0]['labels'].update(severity=2),
            'wrong_api': lambda r: r.update(apiVersion='monitoring.coreos.com/v2'),
            'missing_metadata_labels': lambda r: r['metadata'].pop('labels'),
        }
        for name, change in cases.items():
            variant = deepcopy(rule)
            change(variant)
            with self.subTest(case=name), self.assertRaises(SchemaError):
                check_lab_rule(variant)

    def test_renderer_rejects_a_malformed_rule_before_writing(self):
        from unittest.mock import patch
        from ai_ops import wf10_deployment
        real = wf10_deployment.lab_alert_rule
        def bad(binding):
            spec = real(binding)
            spec['groups'][0]['rules'][0]['keep_firing_for'] = '1m'
            return spec
        out = self.r.root / 'out-bad-rule'
        with patch.object(wf10_deployment, 'lab_alert_rule', side_effect=bad), \
             self.assertRaisesRegex(ValueError, 'wf10_schema_rejected'):
            wf10_deployment.build(self.v, out)
        self.assertFalse(out.exists())

    def test_target_names_are_validated_before_use_in_promql(self):
        binding = deepcopy(self.v['config']['bindings.json']['bindings'][0])
        for key, value in (('namespace', 'ai-ops-lab"} or vector(1) #'), ('workload', 'Lab_API')):
            bad = deepcopy(binding)
            bad['target'][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'lab_alert_target_rejected'):
                lab_alert_rule(bad)


class EventContractTest(unittest.TestCase):
    """The rule's labels must register a lab-target event on the /v1/events path."""
    def setUp(self):
        self.e = events.EventStoreTest()
        self.e.setUp()
        self.addCleanup(self.e.doCleanups)
        self.f, self.store, self.q, self.settings = self.e.f, self.e.store, self.e.q, self.e.settings
        self.count = self.e.count
        target = self.f.target.target.model_dump()
        rule = lab_alert_rule({'target': target})['groups'][0]['rules'][0]
        self.rule_labels, self.target = rule['labels'], target

    def item(self, **changes):
        return fired(self.rule_labels, self.target['workload'], self.target['namespace'], **changes)

    def test_firing_and_resolved_register_one_lab_target_event(self):
        event = self.store.ingest('lab-seoul', self.item())
        view = self.store.show(event)
        self.assertEqual((view['status'], view['alertname'], view['target']), ('firing', LAB_ALERT, self.f.target.alias))
        self.assertEqual(self.count('jobs'), 0)  # manual policy: no automatic investigation
        resolved = self.store.ingest('lab-seoul', self.item(status='resolved', endsAt='1970-01-01T00:20:00Z'))
        self.assertEqual(resolved, event)  # same occurrence (fingerprint + startsAt)
        self.assertEqual(self.store.show(event)['status'], 'resolved')

    def test_wrong_target_labels_register_no_target(self):
        for key, value in (('environment', 'production'), ('service', 'other-svc'), ('workload', 'other')):
            labels = {**self.rule_labels, key: value}
            with self.subTest(key=key):
                item = fired(labels, self.target['workload'], self.target['namespace'],
                             fingerprint=f'{abs(hash(key)) % 16**16:016x}')
                event = self.store.ingest('lab-seoul', item)
                self.assertIsNone(self.store.show(event)['target'])

    def test_legacy_alerts_path_would_reject_it(self):
        # Why the WF10 connection plan switches Alertmanager to /v1/events (ai-events):
        # the legacy /v1/alerts inbox only accepts the five request-metric names.
        inbox = AlertInbox(self.q, self.settings, cluster_id=self.target['cluster_id'])
        with self.assertRaises(RejectedAlert):
            inbox._prepare_alert(self.item(), 1000.0)


class PromtoolDocumentsTest(unittest.TestCase):
    def test_promtool_documents_use_the_rendered_rule_and_contract_labels(self):
        import importlib.util
        from pathlib import Path
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'check_lab_alert_promtool.py'
        spec = importlib.util.spec_from_file_location('check_lab_alert_promtool', script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        group, tests = module.documents()
        self.assertEqual(group, lab_alert_rule({'target': module.TARGET}))
        rule = group['groups'][0]['rules'][0]
        firing = [case for test in tests['tests'] for case in test['alert_rule_test'] if case['exp_alerts']]
        self.assertEqual(len(firing), 2)
        for case in firing:
            [alert] = case['exp_alerts']
            self.assertEqual(alert['exp_labels'], {**rule['labels'], 'namespace': 'ai-ops-lab', 'deployment': 'lab-api'})
            self.assertNotIn('kube-state-metrics', json.dumps(alert))  # scrape labels aggregated away
        self.assertEqual({t['name'] for t in tests['tests'] if not any(c['exp_alerts'] for c in t['alert_rule_test'])},
                         {'scaled to zero on purpose: no alert', 'recovers before for elapses: no alert',
                          'other namespace or deployment: no alert'})


if __name__ == '__main__':
    unittest.main()
