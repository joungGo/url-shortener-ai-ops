"""Check the pinned platform source before local-only template/event tests."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform-root', type=Path, required=True)
    parser.add_argument('--amtool', type=Path, required=True)
    args = parser.parse_args()
    pin = json.loads((ROOT/'integration/wf03-platform-source.json').read_text())
    platform = args.platform_root.resolve()
    revision = subprocess.check_output(['git','rev-parse','HEAD'],cwd=platform,text=True).strip()
    if revision != pin['revision']:
        raise ValueError('WF-03 platform revision mismatch')
    for name, expected in pin['files'].items():
        if hashlib.sha256((platform/name).read_bytes()).hexdigest() != expected:
            raise ValueError('WF-03 platform source hash mismatch')
    tool = args.amtool.resolve(strict=True)
    version = subprocess.check_output([str(tool),'--version'],stderr=subprocess.STDOUT,text=True,timeout=5)
    if 'version 0.32.0' not in version:
        raise ValueError('WF-03 amtool version mismatch')
    os.environ.update(WF03_PLATFORM_ROOT=str(platform),WF03_AMTOOL=str(tool))
    sys.path[:0] = [str(ROOT/'ai-ops/src'),str(ROOT/'ai-ops/tests')]
    suite = unittest.defaultTestLoader.discover(str(ROOT/'ai-ops/tests'),pattern='test_event_notices.py')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() and result.testsRun > 0 and not result.skipped else 1

if __name__ == '__main__':
    raise SystemExit(main())
