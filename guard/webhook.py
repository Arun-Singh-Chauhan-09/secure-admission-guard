"""The Kubernetes AdmissionReview v1 protocol: request in, decision out."""
import json
import threading

from guard.policy import evaluate

MAX_BODY = 3 * 1024 * 1024


class Metrics:
    """Counters for /metrics. A lock, because the HTTP server handles requests in threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self.counts = {"allowed": 0, "denied": 0, "warned": 0, "error": 0}
        self.seconds = 0.0

    def add(self, result):
        with self._lock:
            self.counts[result] += 1

    def observe(self, seconds):
        with self._lock:
            self.seconds += seconds

    def render(self):
        with self._lock:
            lines = ["# HELP admission_guard_decisions_total Admission decisions by result.",
                     "# TYPE admission_guard_decisions_total counter"]
            lines += [f'admission_guard_decisions_total{{result="{k}"}} {v}' for k, v in self.counts.items()]
            lines += ["# HELP admission_guard_seconds_total Time spent handling admission requests.",
                      "# TYPE admission_guard_seconds_total counter",
                      f"admission_guard_seconds_total {self.seconds:.6f}"]
        return "\n".join(lines) + "\n"


def review(body, cfg, enforce=True, metrics=None, log=print):
    """Turn a raw request body into (http_status, response_dict).
    Raises ValueError for input that is not an AdmissionReview (caller answers 400)."""
    if len(body) > MAX_BODY:
        raise ValueError("request too large")
    try:
        req = json.loads(body)["request"]
        uid = req["uid"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"invalid AdmissionReview: {exc}") from exc

    # The response MUST echo the request uid, or the API server rejects it.
    resp = {"uid": uid, "allowed": True}
    result = "allowed"
    if req.get("kind", {}).get("kind") == "Pod" and req.get("operation") in ("CREATE", "UPDATE"):
        pod = req.get("object") or {}
        violations = evaluate(pod, cfg)
        if violations:
            msg = f"admission-guard: {len(violations)} violation(s): " + "; ".join(violations)
            if enforce:
                resp.update(allowed=False, status={"code": 403, "message": msg})
                result = "denied"
            else:
                resp["warnings"] = violations          # audit mode: allowed, kubectl shows warnings
                result = "warned"
            log(json.dumps({"event": "policy_violation", "namespace": req.get("namespace"),
                            "pod": (pod.get("metadata") or {}).get("name"),
                            "enforced": enforce, "violations": violations}))
    if metrics:
        metrics.add(result)
    return 200, {"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview", "response": resp}
