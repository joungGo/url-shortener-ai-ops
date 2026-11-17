"""Optional shared runbook policy for deployed worker and request producers."""
from pathlib import Path


def runbook_settings_kwargs(config: Path) -> dict:
    # A malformed/dangling present policy must fail, never fall back silently.
    path = config / 'runbook-policy.json'
    return {'runbook_policy': path} if path.exists() or path.is_symlink() else {}
