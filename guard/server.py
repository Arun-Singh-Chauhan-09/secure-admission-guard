"""admission-guard: validating admission webhook for pod security and image provenance.

  python -m guard.server --tls-cert /certs/tls.crt --tls-key /certs/tls.key

HTTPS :8443 /validate   called by the Kubernetes API server
HTTP  :8080 /healthz /metrics   probes and Prometheus (plain HTTP, no TLS needed)
"""
import argparse
import json
import os
import signal
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from guard.policy import Config
from guard.webhook import MAX_BODY, Metrics, review


def log(msg):
    print(msg, flush=True)


class CertReloader:
    """Serve the newest certificate files without a restart.

    cert-manager renews the Secret, the kubelet updates the mounted files, and on the
    next TLS handshake we load them into a fresh SSLContext (SNI callback)."""

    def __init__(self, cert, key):
        self.cert, self.key = cert, key
        self._mtime, self._ctx, self._lock = None, None, threading.Lock()

    def context(self):
        mtime = os.stat(self.cert).st_mtime
        with self._lock:
            if self._ctx is None or mtime != self._mtime:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                ctx.load_cert_chain(self.cert, self.key)
                self._ctx, self._mtime = ctx, mtime
                log(json.dumps({"event": "certificate_loaded", "file": self.cert}))
            return self._ctx

    def server_context(self):
        base = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        base.minimum_version = ssl.TLSVersion.TLSv1_2
        base.load_cert_chain(self.cert, self.key)

        def pick(sock, server_name, _ctx):
            sock.context = self.context()   # swap in the current certificate for this handshake
        base.sni_callback = pick
        return base


class TLSServer(ThreadingHTTPServer):
    """HTTPS server that does the TLS handshake in the per-connection thread.

    Wrapping the listening socket would run every handshake inside accept(), on the one
    thread that accepts connections: a single slow or broken client would then stall
    admission for the whole cluster. Here accept() stays plain TCP and each thread does
    its own handshake with a timeout."""

    daemon_threads = True

    def __init__(self, addr, handler, context, handshake_timeout=5):
        super().__init__(addr, handler)
        self.context, self.handshake_timeout = context, handshake_timeout

    def finish_request(self, request, client_address):
        request.settimeout(self.handshake_timeout)
        tls = self.context.wrap_socket(request, server_side=True)   # handshake happens here
        try:
            self.RequestHandlerClass(tls, client_address, self)
        finally:
            tls.close()

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        # Clients that disconnect or fail the handshake are normal noise, not crashes.
        log(json.dumps({"event": "connection_error", "client": client_address[0], "error": repr(exc)}))


def make_handlers(cfg, enforce, metrics):
    class Validate(BaseHTTPRequestHandler):
        def log_message(self, *a):      # no default access log; decisions are logged as JSON
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            start = time.perf_counter()
            if self.path != "/validate" or not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._send(400, b"expected POST /validate application/json", "text/plain")
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                metrics.add("error")
                return self._send(413, b"request too large", "text/plain")
            try:
                code, out = review(self.rfile.read(length), cfg, enforce, metrics, log)
            except ValueError as exc:
                metrics.add("error")
                return self._send(400, str(exc).encode(), "text/plain")
            metrics.observe(time.perf_counter() - start)
            self._send(code, out)

    class Ops(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/healthz":
                body, ctype = b"ok", "text/plain"
            elif self.path == "/metrics":
                body, ctype = metrics.render().encode(), "text/plain; version=0.0.4"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Validate, Ops


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--addr", default="0.0.0.0:8443")
    ap.add_argument("--metrics-addr", default="0.0.0.0:8080")
    ap.add_argument("--tls-cert", default="/certs/tls.crt")
    ap.add_argument("--tls-key", default="/certs/tls.key")
    ap.add_argument("--allowed-registries", default="registry.internal/")
    ap.add_argument("--require-digest", default="true", choices=["true", "false"])
    ap.add_argument("--enforce", default="true", choices=["true", "false"],
                    help="false = audit mode: allow, but return warnings")
    a = ap.parse_args(argv)

    cfg = Config(allowed_registries=[r for r in a.allowed_registries.split(",") if r],
                 require_digest=a.require_digest == "true")
    enforce, metrics = a.enforce == "true", Metrics()
    Validate, Ops = make_handlers(cfg, enforce, metrics)

    host, port = a.addr.rsplit(":", 1)
    https = TLSServer((host, int(port)), Validate, CertReloader(a.tls_cert, a.tls_key).server_context())
    mhost, mport = a.metrics_addr.rsplit(":", 1)
    ops = ThreadingHTTPServer((mhost, int(mport)), Ops)

    for srv in (https, ops):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(json.dumps({"event": "serving", "addr": a.addr, "enforce": enforce, "registries": cfg.allowed_registries}))

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    # Finish in-flight requests; the API server gives up after timeoutSeconds anyway.
    https.shutdown()
    ops.shutdown()
    log(json.dumps({"event": "stopped"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
