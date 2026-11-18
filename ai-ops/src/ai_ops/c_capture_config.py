"""Allow only the established public worker configuration layouts in capture Jobs."""

from copy import deepcopy


# The installed Slack worker uses this fixed, bounded materializer. Never execute
# a command copied from an unvalidated Deployment or inherit its credentials.
COMMON_MATERIALIZE = """import os,pathlib
os.umask(0o077)
source=pathlib.Path('/projected/common');target=pathlib.Path('/out/common')
for p in source.iterdir():
    if p.name.startswith('.'): continue
    raw=p.read_bytes()
    if len(raw)>65536: raise ValueError('configuration_size')
    dest=target/p.name; temp=target/(p.name+'.tmp')
    with open(temp,'wb') as out: out.write(raw)
    os.chmod(temp,0o600);os.replace(temp,dest)
"""
MATERIALIZE_MOUNTS = [
    {"name": "common-projected", "mountPath": "/projected/common", "readOnly": True},
    {"name": "config", "mountPath": "/out/common"},
]
MATERIALIZE_SECURITY = {"allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]}, "readOnlyRootFilesystem": True}
MATERIALIZE_RESOURCES = {"requests": {"cpu": "10m", "memory": "16Mi"},
                         "limits": {"cpu": "100m", "memory": "64Mi"}}


def _require(condition):
    if not condition:
        raise ValueError("unsupported capture configuration layout")


def _projected(volume, name, sources):
    _require(set(volume) == {"name", "projected"} and volume["name"] == name)
    projection = volume["projected"]
    _require(set(projection) <= {"sources", "defaultMode"}
             and projection.get("defaultMode", 420) == 420)
    entries = projection["sources"]
    _require(len(entries) == len(sources))
    names = []
    for entry in entries:
        _require(set(entry) == {"configMap"} and set(entry["configMap"]) == {"name"})
        names.append(entry["configMap"]["name"])
    _require(len(set(names)) == len(names) and set(names) == set(sources))


def capture_configuration(pod, worker):
    """Return public volumes, a fixed initializer, and whether a policy is required.

    Legacy layouts remain supported. The materialized layout must match the
    installed worker's sources, command, mounts, image, and security controls.
    Secrets, service-account identity, env and arbitrary init logic never flow
    into the generated Job.
    """
    try:
        volumes = pod["volumes"]
        by_name = {v["name"]: v for v in volumes}
        _require(len(by_name) == len(volumes))
        config, data = by_name["config"], by_name["data"]
        _require(data == {"name": "data", "persistentVolumeClaim": {"claimName": "ci-queue"}})
        initializers = pod.get("initContainers", [])
        if "emptyDir" not in config:
            _require(not initializers)
            if "configMap" in config:
                _require(set(config) == {"name", "configMap"}
                         and set(config["configMap"]) <= {"name", "defaultMode"}
                         and config["configMap"].get("name") == "ci-config"
                         and config["configMap"].get("defaultMode", 420) == 420)
            else:
                _projected(config, "config", {"ci-config", "ci-normal-binding"})
            return deepcopy([config, data]), [], False

        _require(config == {"name": "config", "emptyDir": {"medium": "Memory", "sizeLimit": "2Mi"}})
        common = by_name["common-projected"]
        _projected(common, "common-projected", {"ci-config-slack-current", "ci-normal-binding"})
        _require([m for m in worker["volumeMounts"] if m.get("name") == "config"] == [
            {"name": "config", "mountPath": "/etc/ai-ops", "readOnly": True}])
        _require(len(initializers) == 1)
        observed = initializers[0]
        fixed = {"name": "common-materialize", "image": worker["image"],
                 "command": ["python", "-c", COMMON_MATERIALIZE],
                 "volumeMounts": MATERIALIZE_MOUNTS, "securityContext": MATERIALIZE_SECURITY,
                 "resources": MATERIALIZE_RESOURCES}
        _require(set(observed) <= set(fixed) | {
            "imagePullPolicy", "terminationMessagePath", "terminationMessagePolicy"})
        _require(all(observed.get(k) == v for k, v in fixed.items()))
        _require(observed.get("imagePullPolicy", "IfNotPresent") == "IfNotPresent"
                 and observed.get("terminationMessagePath", "/dev/termination-log") == "/dev/termination-log"
                 and observed.get("terminationMessagePolicy", "File") == "File")
        return deepcopy([config, data, common]), [deepcopy(fixed)], True
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("invalid capture configuration structure") from exc
