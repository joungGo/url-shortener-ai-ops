"""Explicit loopback fixtures only: no AWS, GitHub, cluster, or real credentials."""
import argparse
import base64
from contextlib import redirect_stderr
import hashlib
import http.server
from io import StringIO
import json
import os
from pathlib import Path
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time

from ai_ops.c_trial_lease_client import CONTEXT, LEASE_PATH, TrialLeaseClient, LeaseRequestError

CANARY = 'LOCAL-SECRET-MUST-NOT-BE-RECORDED'


def check_case(binary, pin, scenario):
    with tempfile.TemporaryDirectory(prefix='c04-stages-', dir='/tmp') as folder:
        root = Path(folder)
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
            '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost',
            '-keyout', str(root/'key'), '-out', str(root/'cert')],
            check=True, capture_output=True, timeout=15)
        credential = root/'credential.py'
        credential.write_text('''import json,os,sys,time
from pathlib import Path
p=Path(__file__).with_name('exec-count');p.write_text(str(int(p.read_text())+1 if p.exists() else 1))
if sys.argv[1]=='authentication':
 parent=os.getppid();end=time.monotonic()+3.25
 while os.getppid()==parent and time.monotonic()<end:time.sleep(.01)
print('LOCAL-SECRET-MUST-NOT-BE-RECORDED',file=sys.stderr)
print(json.dumps({'apiVersion':'client.authentication.k8s.io/v1beta1','kind':'ExecCredential',
 'status':{'token':'LOCAL-SECRET-MUST-NOT-BE-RECORDED'}}))
''')
        state = dict(apiVersion='coordination.k8s.io/v1', kind='Lease', metadata=dict(
            name='b6-scenario-lock', namespace='ai-ops-lab', uid='local-stage-fixture',
            resourceVersion='1'), spec={})
        requests = []
        stop = threading.Event()
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def do_GET(self):
                requests.append(self.command)
                assert self.path == LEASE_PATH
                assert self.headers.get('Authorization') == 'Bearer '+CANARY
                if scenario == 'response_wait':
                    stop.wait(3.25)
                payload = json.dumps(state).encode()
                try:
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(payload)))
                    self.send_header('X-Secret-Canary', CANARY)
                    self.end_headers(); self.wfile.write(payload)
                except (OSError, ssl.SSLError):
                    pass
            def log_message(self, *_):
                pass
        class TLSStall(socketserver.BaseRequestHandler):
            def handle(self):
                stop.wait(3.25)  # Accept TCP; deliberately withhold TLS records locally.
        if scenario == 'tls':
            server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), TLSStall)
            server.daemon_threads = True
        else:
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(root/'cert', root/'key')
            server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = {'apiVersion':'v1', 'kind':'Config', 'current-context':CONTEXT,
            'contexts':[{'name':CONTEXT, 'context':{'cluster':'local','user':'local'}}],
            'clusters':[{'name':'local','cluster':{'server':'https://localhost:'+str(server.server_address[1]),
                'certificate-authority-data':base64.b64encode((root/'cert').read_bytes()).decode()}}],
            'users':[{'name':'local','user':{'exec':{'apiVersion':'client.authentication.k8s.io/v1beta1',
                'command':sys.executable,'args':[str(credential),scenario],'interactiveMode':'Never'}}}]}
        kubeconfig = root/'config'; kubeconfig.write_text(json.dumps(config)); kubeconfig.chmod(0o600)
        client = TrialLeaseClient(kubeconfig, 'local-stage-fixture', trace_binary=binary, trace_sha256=pin)
        output = StringIO()
        try:
            with redirect_stderr(output):
                try:
                    client.start()
                except LeaseRequestError as error:
                    diagnostic = error.diagnostic
                else:
                    raise AssertionError('local_stall_not_rejected')
            trace = diagnostic['upstream']
            assert diagnostic['cause'] == 'timeout', diagnostic
            assert diagnostic['timeout_seconds'] == 3
            assert 2.9 <= diagnostic['elapsed_seconds'] < 4
            assert trace['last_observed'] == {'phase':scenario,'state':'start'}, trace
            assert len({e['request_id'] for e in trace['events']}) == 1
            assert not trace['loss_observed'] and not trace['delivery_complete']
            assert int((root/'exec-count').read_text()) == 1
            assert len(requests) == (1 if scenario == 'response_wait' else 0)
            if scenario == 'response_wait':
                for phase in ('authentication','dns','tcp','tls'):
                    assert any(e['phase']==phase and e['state']=='done' for e in trace['events']), phase
            assert CANARY not in output.getvalue() and CANARY not in json.dumps(diagnostic)
            assert 'localhost' not in output.getvalue() and str(root) not in output.getvalue()
            assert len(output.getvalue().splitlines()) == 1
            assert client.process.poll() is not None and not client.socket_path.parent.exists()
            client.stage_collector.thread.join(timeout=1)
            assert not client.stage_collector.thread.is_alive()
            return {'scenario':scenario, 'diagnostic':diagnostic, 'credential_calls':1,
                    'upstream_http_requests':len(requests), 'secret_canary_absent':True,
                    'proxy_socket_reader_closed':True}
        finally:
            client.close(); stop.set(); server.shutdown(); server.server_close(); thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    assert hashlib.sha256(args.binary.read_bytes()).hexdigest() == args.sha256
    result = [check_case(args.binary, args.sha256, scenario)
              for scenario in ('authentication','tls','response_wait')]
    print(json.dumps({'status':'passed', 'scope':'loopback_only', 'cloud_calls':0,
        'cases':result, 'dns_tcp_pending_coverage':'native_callback_unit_fixtures',
        'binary_sha256':args.sha256}, indent=2))


if __name__ == '__main__':
    main()
