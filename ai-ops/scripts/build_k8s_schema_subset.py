"""Build/check the vendored Kubernetes OpenAPI subset used for offline manifest validation.

Source: kubernetes/kubernetes tag v1.35.8 (same version as the pinned CI kubectl and
the platform EKS 1.35), api/openapi-spec/v3. The three source files are verified
against pinned SHA-256 values before anything is extracted. Only validation keywords
of the transitive closure of the four renderer kinds are kept.

Usage (network only to fetch the public spec files into a directory you choose):
  curl -fsSL -o DIR/<file> https://raw.githubusercontent.com/kubernetes/kubernetes/v1.35.8/api/openapi-spec/v3/<file>
  python ai-ops/scripts/build_k8s_schema_subset.py DIR          # write
  python ai-ops/scripts/build_k8s_schema_subset.py DIR --check  # compare
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

TAG = 'v1.35.8'
SOURCES = {
    'api__v1_openapi.json': '45666551cf0928e4e8a9b88508f9b23aa78d2341aa27b0587457b2694717fcc1',
    'apis__apps__v1_openapi.json': '8f22326dfd8be012bc6247e44bb6adb8070079c0ca9c0a527840811bccf3482e',
    'apis__networking.k8s.io__v1_openapi.json': '6b974984cb19f1ef1832fdfa84b67ce600eff1db354cdb49db0edb1cf87e234f',
}
ROOTS = ['io.k8s.api.core.v1.ConfigMap', 'io.k8s.api.core.v1.Service',
         'io.k8s.api.apps.v1.Deployment', 'io.k8s.api.networking.v1.NetworkPolicy']
KEEP = ('type', 'format', 'properties', 'required', 'items', 'additionalProperties',
        'allOf', 'oneOf', '$ref', 'enum', 'x-kubernetes-group-version-kind')
OUTPUT = Path(__file__).resolve().parents[1] / 'src/ai_ops/k8s_openapi_v1_35_8.json'


def prune(node):
    if isinstance(node, list):
        return [prune(x) for x in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key in KEEP:
        if key not in node:
            continue
        value = node[key]
        if key == 'properties':
            out[key] = {name: prune(schema) for name, schema in value.items()}
        elif key == '$ref':
            out[key] = value.rsplit('/', 1)[-1]
        elif key in ('x-kubernetes-group-version-kind', 'required', 'enum'):
            out[key] = value  # data lists, not schemas: copy verbatim
        else:
            out[key] = prune(value)
    return out


def refs(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == '$ref':
                yield value
            else:
                yield from refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from refs(value)


def build(directory):
    schemas = {}
    for name, expected in SOURCES.items():
        raw = (Path(directory) / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise SystemExit(f'source hash mismatch: {name}')
        schemas.update(json.loads(raw)['components']['schemas'])
    kept, stack = {}, list(ROOTS)
    while stack:
        name = stack.pop()
        if name in kept:
            continue
        kept[name] = prune(schemas[name])
        stack.extend(refs(kept[name]))
    document = {'source': {'repository': 'kubernetes/kubernetes', 'tag': TAG,
                           'path': 'api/openapi-spec/v3', 'sha256': SOURCES},
                'roots': ROOTS, 'schemas': dict(sorted(kept.items()))}
    return (json.dumps(document, sort_keys=True, separators=(',', ':')) + '\n').encode()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('source_dir')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    data = build(args.source_dir)
    if args.check:
        if OUTPUT.read_bytes() != data:
            print('vendored Kubernetes schema subset differs from pinned source', file=sys.stderr)
            return 1
        print('vendored Kubernetes schema subset matches pinned source')
        return 0
    OUTPUT.write_bytes(data)
    print(f'wrote {OUTPUT.name} ({len(data)} bytes, {data.count(b"x-kubernetes-group-version-kind")} kinds)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
