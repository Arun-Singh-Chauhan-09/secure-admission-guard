"""AdmissionReview protocol tests, plus one test over a real HTTPS connection."""
import json
import os
import ssl
import subprocess
import tempfile
import threading
import unittest
import urllib.request

from guard.policy import Config
from guard.server import CertReloader, TLSServer, make_handlers
from guard.webhook import Metrics, review

CFG = Config(allowed_registries=["registry.internal/"], require_digest=True)
BAD_POD = {"metadata": {"name": "bad"}, "spec": {"containers": [{"name": "c", "image": "nginx:latest"}]}}


def body(kind="Pod", op="CREATE", obj=BAD_POD):
    return json.dumps({"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview",
                       "request": {"uid": "abc-123", "kind": {"kind": kind}, "namespace": "team-a",
                                   "operation": op, "object": obj}}).encode()


def quiet(*_):
    pass


class ReviewTests(unittest.TestCase):
    def test_deny_echoes_uid_and_returns_403_with_reasons(self):
        code, out = review(body(), CFG, enforce=True, log=quiet)
        r = out["response"]
        self.assertEqual((code, out["apiVersion"], out["kind"]), (200, "admission.k8s.io/v1", "AdmissionReview"))
        self.assertEqual(r["uid"], "abc-123")
        self.assertFalse(r["allowed"])
        self.assertEqual(r["status"]["code"], 403)
        self.assertIn("digest", r["status"]["message"])

    def test_audit_mode_allows_with_warnings(self):
        m = Metrics()
        _, out = review(body(), CFG, enforce=False, metrics=m, log=quiet)
        self.assertTrue(out["response"]["allowed"])
        self.assertTrue(out["response"]["warnings"])
        self.assertEqual((m.counts["warned"], m.counts["denied"]), (1, 0))

    def test_non_pod_and_delete_are_allowed(self):
        for kind, op in (("Deployment", "CREATE"), ("Pod", "DELETE")):
            self.assertTrue(review(body(kind, op), CFG, log=quiet)[1]["response"]["allowed"])

    def test_garbage_input_raises_value_error(self):
        for bad in (b"{not json", b'{"no":"request"}', b'{"request":{}}'):
            with self.assertRaises(ValueError):
                review(bad, CFG, log=quiet)

    def test_metrics_render(self):
        m = Metrics()
        review(body(), CFG, metrics=m, log=quiet)
        self.assertIn('admission_guard_decisions_total{result="denied"} 1', m.render())


def make_cert(directory, cn):
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-keyout", os.path.join(directory, "tls.key"), "-out", os.path.join(directory, "tls.crt"),
                    "-subj", f"/CN={cn}", "-addext", "subjectAltName=DNS:localhost"],
                   check=True, capture_output=True)


class HttpsTests(unittest.TestCase):
    """The real server path: TLS handshake, HTTP handler, JSON over the wire, cert rotation."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        make_cert(self.dir.name, "first")
        self.cert = os.path.join(self.dir.name, "tls.crt")
        self.reloader = CertReloader(self.cert, os.path.join(self.dir.name, "tls.key"))
        Validate, _ = make_handlers(CFG, True, Metrics())
        self.srv = TLSServer(("127.0.0.1", 0), Validate, self.reloader.server_context(), handshake_timeout=1)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"https://localhost:{self.srv.server_address[1]}/validate"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.dir.cleanup()

    def post(self):
        ctx = ssl.create_default_context(cafile=self.cert)
        req = urllib.request.Request(self.url, data=body(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, context=ctx, timeout=5) as resp:
            return json.loads(resp.read()), resp

    def peer_cn(self):
        ctx = ssl.create_default_context(cafile=self.cert)
        import socket
        with socket.create_connection(("localhost", self.srv.server_address[1])) as s:
            with ctx.wrap_socket(s, server_hostname="localhost") as t:
                return dict(x[0] for x in t.getpeercert()["subject"])["commonName"]

    def test_denied_over_tls(self):
        out, _ = self.post()
        self.assertFalse(out["response"]["allowed"])

    def test_stuck_client_does_not_block_others(self):
        import socket
        stuck = socket.create_connection(("localhost", self.srv.server_address[1]))  # connects, never handshakes
        try:
            out, _ = self.post()            # must still be answered immediately
            self.assertFalse(out["response"]["allowed"])
        finally:
            stuck.close()

    def test_rotated_certificate_is_served_without_restart(self):
        self.assertEqual(self.peer_cn(), "first")
        st = os.stat(self.cert)
        make_cert(self.dir.name, "rotated")
        os.utime(self.cert, (st.st_atime + 5, st.st_mtime + 5))   # make sure mtime changes
        self.assertEqual(self.peer_cn(), "rotated")


if __name__ == "__main__":
    unittest.main()
