"""Build outside the repository; never downloads/builds on a guard's start path."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def patch_source(source):
    edits = [
        ('func (r *roundTripper) RoundTrip(req *http.Request) (*http.Response, error) {',
         'func (r *roundTripper) RoundTrip(req *http.Request) (*http.Response, error) {\n\tobserver := newLeaseTrace(req)\n\treq = observer.withContext(req)'),
        ('\tcreds, err := r.a.getCreds()\n\tif err != nil {',
         '\tobserver.stage("authentication", "start", nil)\n\tcreds, err := r.a.getCreds()\n\tobserver.finish("authentication", err)\n\tif err != nil {'),
        ('\tres, err := r.base.RoundTrip(req)\n\tif err != nil {',
         '\tres, err := r.base.RoundTrip(req)\n\tobserver.finish("response_headers", err)\n\tif err != nil {'),
        ('\t\tif err := r.a.maybeRefreshCreds(creds); err != nil {',
         '\t\tobserver.stage("authentication_refresh", "start", nil)\n\t\trefreshErr := r.a.maybeRefreshCreds(creds)\n\t\tobserver.finish("authentication_refresh", refreshErr)\n\t\tif err := refreshErr; err != nil {'),
    ]
    for old, new in edits:
        if source.count(old) != 1:
            raise ValueError('unsupported_client_go_source')
        source = source.replace(old,new)
    return source


def locked_dependencies(dest):
    return {name: (dest/name).read_bytes() for name in ('go.mod', 'go.sum')}


def check_locked_dependencies(dest, expected):
    if locked_dependencies(dest) != expected:
        raise ValueError('lease_trace_dependency_lock_changed')


def main():
    p=argparse.ArgumentParser();p.add_argument('--go',default='go');p.add_argument('--output',type=Path,required=True)
    p.add_argument('--offline', action='store_true', help='Use cached modules and the explicit local Go toolchain only')
    args=p.parse_args();root=Path(__file__).resolve().parent;dest=args.output.resolve()
    dest.mkdir(parents=True,exist_ok=False,mode=0o700)
    for n in ('go.mod','go.sum','main.go'):
        if (root/n).exists():shutil.copyfile(root/n,dest/n)
    env = dict(os.environ, GOTOOLCHAIN='local')
    if args.offline:
        env.update(GOPROXY='off', GOSUMDB='off', GONOPROXY='none')
    def run(*argv):return subprocess.check_output([args.go,*argv],cwd=dest,text=True,env=env)
    original_locks = locked_dependencies(dest)
    meta=json.loads(run('mod','download','-json','k8s.io/client-go@v0.34.1'))
    if meta.get('Version')!='v0.34.1' or meta.get('Sum') != 'h1:ZUPJKgXsnKwVwmKKdPfw4tB58+7/Ik3CrjOEhsiZ7mY=':
        raise ValueError('client_go_module_not_verified')
    check_locked_dependencies(dest, original_locks)
    # Verify cached original sources before replacing client-go with a private copy.
    run('mod','verify')
    # Go authenticates modules with go.sum/checksum database. Patch a private copy only.
    shutil.copytree(meta['Dir'],dest/'client-go')
    for path in (dest/'client-go').rglob('*'):
        path.chmod(0o700 if path.is_dir() or path.stat().st_mode & 0o111 else 0o600)
    (dest/'client-go').chmod(0o700)
    original=dest/'client-go/plugin/pkg/client/auth/exec/exec.go'
    original.chmod(0o600);source=original.read_text()
    if hashlib.sha256(source.encode()).hexdigest() != 'bfea9be50246b1ba2baa9407d5e816a52252bb6590b86b28dbdf5dccf638324c':
        raise ValueError('unexpected_client_go_exec_source')
    original.write_text(patch_source(source))
    for n in ('lease_trace.go','lease_trace_test.go'):
        if (root/n).exists():shutil.copyfile(root/n,original.parent/n)
    run('mod','edit','-replace=k8s.io/client-go=./client-go')
    runtime_locks = locked_dependencies(dest)
    # Go module archives omit executable bits on upstream shell test fixtures.
    (original.parent/'testdata/test-plugin.sh').chmod(0o700)
    run('test','-mod=readonly','k8s.io/client-go/plugin/pkg/client/auth/exec','-count=1')
    check_locked_dependencies(dest, runtime_locks)
    run('build','-mod=readonly','-trimpath','-o','kubectl-c04-trace','.')
    check_locked_dependencies(dest, runtime_locks)
    binary=dest/'kubectl-c04-trace';binary.chmod(0o700)
    manifest={'schema_version':1,'offline':args.offline,'dependency_lock_unchanged':True,'upstream_client_go':'v0.34.1','upstream_module_sum':meta['Sum'],
              'original_exec_sha256':hashlib.sha256(source.encode()).hexdigest(),
              'source_hashes':{n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in ('lease_trace.go','lease_trace_test.go','main.go','build.py','go.mod','go.sum')},
              'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'go_version':run('version').strip()}
    (dest/'build-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps(manifest))


if __name__=='__main__':main()
