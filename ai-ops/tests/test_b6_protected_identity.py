"""Lab protected-object identity without the B-path premise (WF11 follow-up, 2026-11-27). Fake kubectl."""
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from ai_ops import b6_lab
from ai_ops.b6_lab import CONTEXT, PROTECTED_OPTIONAL, PROTECTED_REQUIRED, protected_identity
from ai_ops.b6_scenarios import LabClient

APP = ("url-shortener", "deployment", "url-shortener-api")
WF10 = (("ai-ops", "deployment", "ai-ops"), ("ai-ops", "pvc", "ai-ops-data"))
B = (("ai-ops", "statefulset", "ai-ops"), ("ai-ops", "pvc", "data-ai-ops-0"))


def obj(name, generation=1):
    return {"metadata": {"name": name, "uid": "uid-" + name, "generation": generation}}


class Cluster:
    """Answers `kubectl ... -n NS get KIND NAME -o json [--ignore-not-found]` like kubectl does."""

    def __init__(self, *present, generations=None):
        self.objects = {item: obj("-".join(item), (generations or {}).get(item, 1)) for item in present}
        self.calls = []

    def __call__(self, *args, **kw):
        self.calls.append(args)
        rest = list(args)
        if rest[:2] == ["kubectl", "--context"]:
            rest = rest[3:]
        ignore = rest[-1:] == ["--ignore-not-found"]
        if ignore:
            rest = rest[:-1]
        assert rest[0] == "-n" and rest[2] == "get" and rest[-2:] == ["-o", "json"], rest
        item = (rest[1], rest[3], rest[4])
        if item in self.objects:
            return json.dumps(self.objects[item])
        if ignore:
            return ""
        raise ValueError("kubectl get failed")  # NotFound without --ignore-not-found


def deploy_module():
    script = Path(__file__).resolve().parents[1] / "scripts/deploy_b6_lab.py"
    spec = importlib.util.spec_from_file_location("deploy_lab_protected", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProtectedIdentityTest(unittest.TestCase):
    def test_list_is_the_app_plus_optional_ai_service_objects(self):
        self.assertEqual(PROTECTED_REQUIRED, (APP,))
        self.assertEqual(set(PROTECTED_OPTIONAL), set(WF10 + B))

    def test_wf10_only_install_passes_and_records_the_b_worker_as_absent(self):
        cluster = Cluster(APP, *WF10)
        got = protected_identity(lambda *a: cluster(*a))
        self.assertEqual(got["url-shortener/deployment/url-shortener-api"],
                         {"uid": "uid-url-shortener-deployment-url-shortener-api", "generation": 1})
        self.assertEqual(got["ai-ops/deployment/ai-ops"]["uid"], "uid-ai-ops-deployment-ai-ops")
        self.assertIn("uid", got["ai-ops/pvc/ai-ops-data"])
        self.assertEqual((got["ai-ops/statefulset/ai-ops"], got["ai-ops/pvc/data-ai-ops-0"]),
                         ({"absent": True}, {"absent": True}))
        self.assertEqual(len(got), 5)  # Deployment and StatefulSet named ai-ops do not collide
        for call in cluster.calls:
            self.assertEqual(call[2], "get")
            self.assertEqual("--ignore-not-found" in call, tuple(call[1:2] + call[3:5]) != APP)

    def test_b_install_and_lab_before_wf10_also_pass(self):
        got = protected_identity(Cluster(APP, *B))
        self.assertIn("uid", got["ai-ops/statefulset/ai-ops"])
        self.assertEqual(got["ai-ops/deployment/ai-ops"], {"absent": True})
        got = protected_identity(Cluster(APP))
        self.assertEqual([k for k, v in got.items() if v == {"absent": True}], ["/".join(x) for x in PROTECTED_OPTIONAL])

    def test_any_change_between_two_reads_is_detected(self):
        before = protected_identity(Cluster(APP, *WF10))
        changes = {'appears': Cluster(APP, *WF10, B[0]), 'disappears': Cluster(APP, WF10[0]),
                   'generation': Cluster(APP, *WF10, generations={WF10[0]: 2}),
                   'app_generation': Cluster(APP, *WF10, generations={APP: 3})}
        for name, cluster in changes.items():
            with self.subTest(change=name):
                self.assertNotEqual(protected_identity(cluster), before)
        self.assertEqual(protected_identity(Cluster(APP, *WF10)), before)

    def test_missing_production_app_still_fails(self):
        with self.assertRaises(ValueError):
            protected_identity(Cluster(*WF10))
        with self.assertRaisesRegex(ValueError, "protected_resource_missing"):
            protected_identity(lambda *a: "")  # empty answer for the required object

    def test_lab_install_and_lab_client_use_the_same_definition(self):
        module = deploy_module()
        cluster = Cluster(APP, *WF10)
        with patch.object(module, "command", side_effect=cluster):
            from_install = module.production_identity()
        self.assertTrue(all(c[:3] == ("kubectl", "--context", CONTEXT) for c in cluster.calls))
        runs = []

        def run(args, **kw):
            runs.append(args)
            try:
                return subprocess.CompletedProcess(args, 0, cluster(*args), "")
            except ValueError:
                return subprocess.CompletedProcess(args, 1, "", "NotFound")

        with patch.object(subprocess, "run", side_effect=run):
            from_client = LabClient().production()
        self.assertEqual(from_install, from_client)
        self.assertEqual(len(runs), 5)
        self.assertTrue(all(r[:3] == ["kubectl", "--context", CONTEXT] for r in runs))
        self.assertIs(module.protected_identity, b6_lab.protected_identity)

    def test_install_fails_when_a_protected_object_changes_during_the_install(self):
        module = deploy_module()
        before = protected_identity(Cluster(APP, *WF10))
        after = Cluster(APP, *WF10, generations={WF10[1]: 2})
        scoped = lambda *a, **k: '{"items": []}'
        with patch.object(module, "command", side_effect=after), \
                self.assertRaisesRegex(ValueError, "운영 리소스 식별자"):
            module._finish_deployment(scoped, "{}", before, {}, "0" * 64)


if __name__ == "__main__":
    unittest.main()
