"""Offline structural validation of rendered Kubernetes manifests.

Uses the vendored Kubernetes v1.35.8 OpenAPI v3 subset (``k8s_openapi_v1_35_8.json``,
built and hash-checked by ``ai-ops/scripts/build_k8s_schema_subset.py``). Unknown
fields are rejected like ``kubectl --validate=strict``; types, required fields,
int32/int64 ranges and int-or-string unions are checked.

This is NOT a server dry-run: no admission, defaulting, cross-field, name-format or
quota checks, and no cluster access. Errors carry the field path, never values.
"""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path

DATA = Path(__file__).with_name('k8s_openapi_v1_35_8.json')
RANGES = {'int32': (-2**31, 2**31 - 1), 'int64': (-2**63, 2**63 - 1)}
TYPES = {
    'object': lambda v: isinstance(v, dict),
    'array': lambda v: isinstance(v, list),
    'string': lambda v: isinstance(v, str),
    'boolean': lambda v: isinstance(v, bool),
    'integer': lambda v: isinstance(v, int) and not isinstance(v, bool),
    'number': lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
}


class SchemaError(ValueError):
    pass


@lru_cache(maxsize=1)
def document():
    return json.loads(DATA.read_bytes())


def _check(value, schema, path, schemas, depth=0):
    if depth > 64:
        raise SchemaError(path + ': nesting too deep')
    if '$ref' in schema:
        _check(value, schemas[schema['$ref']], path, schemas, depth + 1)
    for sub in schema.get('allOf', ()):
        _check(value, sub, path, schemas, depth + 1)
    if 'oneOf' in schema:
        matches = 0
        for sub in schema['oneOf']:
            try:
                _check(value, sub, path, schemas, depth + 1)
                matches += 1
            except SchemaError:
                pass
        if matches != 1:
            raise SchemaError(path + ': no single matching alternative')
    kind = schema.get('type')
    if kind is not None and not TYPES[kind](value):
        raise SchemaError(f'{path}: expected {kind}')
    if kind == 'integer' and schema.get('format') in RANGES:
        low, high = RANGES[schema['format']]
        if not low <= value <= high:
            raise SchemaError(f'{path}: out of {schema["format"]} range')
    if 'enum' in schema and value not in schema['enum']:
        raise SchemaError(path + ': not an allowed value')
    if isinstance(value, dict) and ('properties' in schema or 'additionalProperties' in schema):
        properties = schema.get('properties', {})
        extra = schema.get('additionalProperties')
        for name in schema.get('required', ()):
            if name not in value:
                raise SchemaError(f'{path}.{name}: required')
        for name, item in value.items():
            child = f'{path}.{name}'
            if name in properties:
                _check(item, properties[name], child, schemas, depth + 1)
            elif isinstance(extra, dict):
                _check(item, extra, child, schemas, depth + 1)
            elif extra is not True:
                raise SchemaError(child + ': unknown field')
    if isinstance(value, list) and 'items' in schema:
        for index, item in enumerate(value):
            _check(item, schema['items'], f'{path}[{index}]', schemas, depth + 1)


def root_for(api_version, kind):
    group, _, version = api_version.rpartition('/')
    for name in document()['roots']:
        schema = document()['schemas'][name]
        for gvk in schema.get('x-kubernetes-group-version-kind', ()):
            if (gvk['group'], gvk['version'], gvk['kind']) == (group, version, kind):
                return name
    raise SchemaError(f'{kind}: unsupported apiVersion/kind')


def validate_manifest(manifest):
    """Raise SchemaError if the manifest is not structurally valid for v1.35."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get('apiVersion'), str) \
            or not isinstance(manifest.get('kind'), str):
        raise SchemaError('manifest: apiVersion/kind required')
    name = root_for(manifest['apiVersion'], manifest['kind'])
    schemas = document()['schemas']
    _check(manifest, schemas[name], manifest['kind'], schemas)
