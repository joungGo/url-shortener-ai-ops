"""Offline tests for the diagnostic builder's source and lock boundaries."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

PATH = Path(__file__).resolve().parents[1]/'tools/lease-trace-kubectl/build.py'
spec = importlib.util.spec_from_file_location('lease_trace_build', PATH)
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class LeaseTraceBuildTest(unittest.TestCase):
    def test_dependency_drift_is_rejected_without_exposing_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('go.mod', 'go.sum'):
                (root/name).write_text('original-lock')
            expected = build.locked_dependencies(root)
            build.check_locked_dependencies(root, expected)
            for name in ('go.mod', 'go.sum'):
                (root/name).write_text('PRIVATE-CANARY')
                with self.assertRaisesRegex(ValueError, '^lease_trace_dependency_lock_changed$'):
                    build.check_locked_dependencies(root, expected)
                (root/name).write_bytes(expected[name])

    def test_patch_requires_exact_upstream_call_sites(self):
        source = '''func (r *roundTripper) RoundTrip(req *http.Request) (*http.Response, error) {
\tcreds, err := r.a.getCreds()
\tif err != nil {
\tres, err := r.base.RoundTrip(req)
\tif err != nil {
\t\tif err := r.a.maybeRefreshCreds(creds); err != nil {'''
        patched = build.patch_source(source)
        self.assertEqual(patched.count('r.a.getCreds()'), 1)
        self.assertEqual(patched.count('r.base.RoundTrip(req)'), 1)
        self.assertEqual(patched.count('r.a.maybeRefreshCreds(creds)'), 1)
        self.assertNotIn('WithTimeout', patched)
        self.assertNotIn('Sleep', patched)
        for bad in ('unsupported-source', source+source, patched):
            with self.assertRaisesRegex(ValueError, '^unsupported_client_go_source$'):
                build.patch_source(bad)


if __name__ == '__main__':
    unittest.main()
