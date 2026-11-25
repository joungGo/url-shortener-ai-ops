"""Offline promtool check of the WF11 lab alert rule (renderer output, label contract).

Writes the exact rule group produced by ``wf10_deployment.lab_alert_rule`` and a
``promtool test rules`` file into DIR, then runs ``promtool check rules`` and
``promtool test rules`` when ``--promtool`` points to a promtool binary. No cluster,
no network. promtool itself is not vendored: use an official release binary whose
SHA-256 you verified.

  PYTHONPATH=ai-ops/src python ai-ops/scripts/check_lab_alert_promtool.py DIR [--promtool PATH]

Scenarios (kube-state-metrics series carry scrape labels such as service/pod/job,
which the rule must aggregate away so the target labels come only from the rule):
  1. desired 1, available 1 -> 0 at 1m: pending until 3m, firing with the contract labels;
  2. two kube-state-metrics replicas reporting the same Deployment: still one alert;
  3. desired 0 (intentionally scaled down), available 0: never fires;
  4. available recovers before `for` elapses: never fires.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from ai_ops.wf10_deployment import LAB_ALERT, lab_alert_rule

TARGET = {'cluster_id': 'urlshortener', 'namespace': 'ai-ops-lab', 'workload': 'lab-api',
          'service': 'lab-api', 'environment': 'practice'}
SCRAPE = {'job': 'kube-state-metrics', 'service': 'kube-prometheus-stack-kube-state-metrics',
          'endpoint': 'http', 'instance': '10.0.0.10:8080', 'pod': 'ksm-0', 'container': 'kube-state-metrics'}


def series(metric, values, namespace='ai-ops-lab', deployment='lab-api', **extra):
    labels = {**SCRAPE, **extra, 'namespace': namespace, 'deployment': deployment}
    body = ','.join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return {'series': f'{metric}{{{body}}}', 'values': values}


def documents(target=TARGET):
    group = lab_alert_rule({'target': target})
    rule = group['groups'][0]['rules'][0]
    labels = {**rule['labels'], 'namespace': target['namespace'], 'deployment': target['workload']}
    alert = {'exp_labels': labels, 'exp_annotations': rule['annotations']}
    tests = [
        {'name': 'image pull failure: pending then firing with contract labels', 'interval': '30s',
         'input_series': [series('kube_deployment_spec_replicas', '1x20'),
                          series('kube_deployment_status_replicas_available', '1 1 0x18')],
         'alert_rule_test': [{'eval_time': '2m30s', 'alertname': LAB_ALERT, 'exp_alerts': []},
                             {'eval_time': '3m30s', 'alertname': LAB_ALERT, 'exp_alerts': [alert]}]},
        {'name': 'two kube-state-metrics replicas: one alert', 'interval': '30s',
         'input_series': [series('kube_deployment_spec_replicas', '1x20'),
                          series('kube_deployment_spec_replicas', '1x20', pod='ksm-1', instance='10.0.0.11:8080'),
                          series('kube_deployment_status_replicas_available', '0x20'),
                          series('kube_deployment_status_replicas_available', '0x20', pod='ksm-1',
                                 instance='10.0.0.11:8080')],
         'alert_rule_test': [{'eval_time': '4m', 'alertname': LAB_ALERT, 'exp_alerts': [alert]}]},
        {'name': 'scaled to zero on purpose: no alert', 'interval': '30s',
         'input_series': [series('kube_deployment_spec_replicas', '0x20'),
                          series('kube_deployment_status_replicas_available', '0x20')],
         'alert_rule_test': [{'eval_time': '5m', 'alertname': LAB_ALERT, 'exp_alerts': []}]},
        {'name': 'recovers before for elapses: no alert', 'interval': '30s',
         'input_series': [series('kube_deployment_spec_replicas', '1x20'),
                          series('kube_deployment_status_replicas_available', '1 0 0 0 1x16')],
         'alert_rule_test': [{'eval_time': '3m', 'alertname': LAB_ALERT, 'exp_alerts': []}]},
        {'name': 'other namespace or deployment: no alert', 'interval': '30s',
         'input_series': [series('kube_deployment_spec_replicas', '1x20', deployment='other-api'),
                          series('kube_deployment_status_replicas_available', '0x20', deployment='other-api'),
                          series('kube_deployment_spec_replicas', '1x20', namespace='url-shortener'),
                          series('kube_deployment_status_replicas_available', '0x20', namespace='url-shortener')],
         'alert_rule_test': [{'eval_time': '5m', 'alertname': LAB_ALERT, 'exp_alerts': []}]},
    ]
    return group, {'rule_files': ['lab-alert-rules.json'], 'evaluation_interval': '30s', 'tests': tests}


def main(argv=None):
    parser = argparse.ArgumentParser(description='promtool check/test of the WF11 lab alert rule')
    parser.add_argument('directory', type=Path)
    parser.add_argument('--promtool', type=Path)
    args = parser.parse_args(argv)
    args.directory.mkdir(mode=0o700, exist_ok=False)
    group, tests = documents()
    # promtool reads YAML; JSON is valid YAML.
    (args.directory / 'lab-alert-rules.json').write_text(json.dumps(group, ensure_ascii=False, indent=2))
    (args.directory / 'lab-alert-tests.json').write_text(json.dumps(tests, ensure_ascii=False, indent=2))
    if args.promtool is None:
        print(json.dumps({'status': 'written', 'files': ['lab-alert-rules.json', 'lab-alert-tests.json']}))
        return 0
    results = {}
    for name, command in (('check', ['check', 'rules', 'lab-alert-rules.json']),
                          ('test', ['test', 'rules', 'lab-alert-tests.json'])):
        done = subprocess.run([str(args.promtool), *command], cwd=args.directory, capture_output=True,
                              text=True, timeout=60)
        results[name] = done.returncode
        sys.stdout.write(done.stdout[-4000:])
        sys.stderr.write(done.stderr[-4000:])
    print(json.dumps({'status': 'passed' if not any(results.values()) else 'failed', 'returncodes': results}))
    return 0 if not any(results.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
