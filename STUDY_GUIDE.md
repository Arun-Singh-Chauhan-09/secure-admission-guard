# Study guide: Helsing Site Reliability Engineer

## 60-second pitch
> "You run Kubernetes on-prem in high-security environments with policy-as-code and supply-chain controls. I wrote a validating admission webhook in Python, using only the standard library, that only admits digest-pinned images from the internal registry, running as non-root. Then I reproduced the failure mode that worries me most about such controls. A fail-closed webhook with one replica got drained during patching, couldn't recreate itself because it was guarding its own namespace, and blocked every pod create in the cluster, CoreDNS included. I kept it fail-closed but contained the blast radius: exempt its own namespace and kube-system by an immutable label, 2 replicas with anti-affinity and a PDB, a 3 s timeout, hot-reloaded certs, and TLS handshakes that can't block each other. CI rejects any webhook config that repeats it."

## Concepts
- **Admission chain:** authn → authz → mutating webhooks → schema validation → validating webhooks → etcd. Validating webhooks can't change objects; mutating ones can.
- **AdmissionReview v1:** the request has `uid`, `kind`, `operation`, `object` (and `oldObject` on UPDATE). The response **must echo `uid`**, plus `allowed` and `status{code,message}`. `warnings` show in kubectl even when allowed (audit mode).
- **failurePolicy:** `Fail` = deny when the webhook is unreachable (secure, risky). `Ignore` = allow (available, but unsafe for security controls).
- **namespaceSelector:** `kubernetes.io/metadata.name` is set by the API server and can't be faked, so use it for exemptions. A custom "exempt" label is a bypass for anyone who can label namespaces.
- **sideEffects: None:** required for `kubectl --dry-run=server`.
- **Why validate Pods:** every controller (Deployment, Job, operator) ends in a Pod create. The downside is that errors show in ReplicaSet events, not in `kubectl apply`.
- **Digest vs tag:** `@sha256:...` is immutable; a tag can be moved. Signing (cosign) proves *who* built an image; a digest proves *what* it is.
- **PDB + anti-affinity:** a PDB limits voluntary evictions; anti-affinity keeps the two replicas on different nodes. You need both.

## File by file (Python)
### `guard/policy.py`
- `Config` is a `@dataclass`. `field(default_factory=...)` gives each instance its own list, because a mutable default would be shared between instances.
- `_containers(spec)` is a **generator** (`yield`). It walks init, normal and ephemeral containers. `spec.get(key) or []` handles both a missing key and `null`.
- `_check_image`: `startswith(prefix)` with the trailing `/` stops `registry.internal.evil.com`. `DIGEST.search` is a regex for `@sha256:` + 64 hex characters at the end.
- `evaluate`: collects **every** violation in a list. `sc.get("allowPrivilegeEscalation") is not False` is deliberate: Kubernetes allows escalation unless it is explicitly `false`, so a missing value counts as a violation. Pod-level `runAsNonRoot: true` counts for all containers.

### `guard/webhook.py`
- `review(body, cfg, enforce, metrics, log)` is pure logic: bytes in, `(status, dict)` out. That makes it easy to test without any network.
- Bad JSON or a missing `request`/`uid` raises `ValueError`, and the server turns that into HTTP 400. `raise ... from exc` keeps the original error.
- Only Pod CREATE/UPDATE are evaluated; everything else is allowed.
- `Metrics` uses a `threading.Lock` because requests run in parallel threads. Without it, two threads doing `+= 1` can lose an update.

### `guard/server.py`
- `ThreadingHTTPServer`: one thread per connection. `daemon_threads = True` means the threads don't block process exit.
- **`TLSServer.finish_request`** wraps each accepted socket in TLS **inside the connection's own thread**, with a 5 s timeout. If you wrap the *listening* socket instead, every handshake runs in `accept()` on the single accept thread, and one stuck client stalls admission for the whole cluster. (`test_stuck_client_does_not_block_others` proves it.)
- **`CertReloader`**: `sni_callback` runs on every TLS handshake. If the certificate file's mtime changed, it builds a new `SSLContext` and swaps it into the connection (`sock.context = ...`). That's how a cert-manager renewal is picked up without a restart.
- Two servers: HTTPS `:8443/validate` for the API server, and plain HTTP `:8080` for `/healthz` and `/metrics`, so probes don't need TLS.
- **SIGTERM:** a `threading.Event` waits, the signal handler sets it, then `shutdown()` stops both servers. Measured: a clean exit 0.56 s after SIGTERM.

### `deploy/fixed`, `tools/`, `drill/`
- Same as the README: the safe config, a checker that follows webhook → Service → Deployment → PDB, and the kind drill.

## Likely questions
1. **"Fail open or fail closed?"** Closed for security controls, but engineered so the webhook can't block its own recovery or the control plane: exemptions, HA, timeouts, monitoring, a documented break-glass.
2. **"Python is slow. Is that a problem for a webhook?"** The work per request is tiny (a JSON parse and a few checks, well under a millisecond), so TLS and the network dominate. With threads, 2 replicas and a 3 s API-server timeout it's fine for most clusters. At very high pod churn I'd measure p99 and consider Go, or Gatekeeper/Kyverno.
3. **"How do you roll out a new policy without breaking teams?"** Audit mode first (`--enforce=false` gives warnings + metrics), fix violations with the teams, then enforce namespace by namespace.
4. **"Gatekeeper vs Kyverno vs your own webhook?"** Gatekeeper: Rego, constraint templates, audit of existing objects. Kyverno: YAML policies, mutation, image verification. Your own: full control, but you own the availability. In production I'd use one of the two; writing my own taught me the failure modes.
5. **"Supply-chain controls end to end?"** CI build → SBOM → scan (Trivy) → sign (cosign) → push by digest to an internal registry → admission checks registry, digest and signature → Falco at runtime.
6. **"Why threads and not asyncio?"** Each request is short and CPU-light. Threads with the standard-library server keep the code small and readable. Handshake timeouts matter more than raw concurrency here.

7. **"Tell me about a time a security control blocked you."** My own CI, on day one: the Trivy action couldn't start because one of *its* dependencies was pulled by a tag that had been removed, so I learned that pinning must be transitive. Then the scan blocked my image on 5 HIGH CVEs inside pip's bundled libraries. My service never uses pip, so I removed pip from the runtime image instead of writing exceptions. Fix the cause, don't silence the alarm.

8. **"Tell me about a bug your tests missed."** The kind drill. The API server calls `/validate?timeout=10s`; my handler compared the full path, so every real call was rejected with 400 and the connection was reset. Unit tests and curl used `/validate` without a query, so they passed. Worse, my CI check grepped for "denied", which matched the drill's own heading, so CI was green. Fix: ignore the query string, add a test with the real URL, and make the drill fail unless the webhook returns `denied the request`. Lesson: test against the real caller, and make sure every check can actually fail.

9. **"Is your own image secure?"** I reviewed it like a security team would. My base image was pinned by tag while my webhook demands digests, so I pinned it by digest, made CI fail on an unpinned `FROM`, and added Dependabot so the pin gets updated. I also added a default-deny NetworkPolicy (8443 open because the API server isn't a pod; 8080 only from monitoring; no egress) and a PriorityClass so the guard isn't evicted before the apps it protects.

10. **"How do you know it's healthy, and who gets woken up?"** SLI is the share of API server → webhook calls that got an answer; a denial counts as success, because that's the guard working. SLO 99.9 %. Burn-rate alerts on two windows: 14.4× pages, 6× opens a ticket. Plus `AdmissionGuardDown`, measured as "no replica scraped", since a dead guard can't report its own errors; the API server's `apiserver_admission_webhook_rejection_count{error_type="calling_webhook_error"}` is the outside view. Alertmanager routes critical to on-call and inhibits the matching warning. Every alert links to a runbook.
11. **"Why Terraform *and* Argo CD?"** Terraform installs the platform once (charts, versions, values). Argo CD deploys what changes often (the guard, its alerts, its dashboard) from Git, and self-heals drift. One gotcha I hit in design: cert-manager writes the CA into the webhook config, so Argo CD must ignore that field, or it would keep removing it and break every webhook call.
12. **"Metrics or logs for this?"** Both, for different questions. Metrics (Prometheus) answer *is it healthy and how fast is the budget burning*: cheap, aggregated, drive alerts. Logs (Loki) answer *who and what*: which namespace, which pod, which rule. The guard logs one JSON line per violation; LogQL `| json` turns it into labels at query time, so I can chart denials by team without adding high-cardinality labels to Prometheus. Alloy (Promtail's successor) ships the logs via the Kubernetes API.

## Honest gaps to name
Service mesh in production, Falco, distributed tracing (Tempo/OpenTelemetry; Loki is done), and MLOps platforms (Kubeflow/MLflow). Also ask: "Defence work may need security vetting. What's the process for someone who isn't an EU citizen?"

## Before the interview
- [ ] `python3 -m unittest discover -s tests -t . -v`, then explain `evaluate`, `review`, `TLSServer.finish_request` and the `urlsplit` fix without notes
- [ ] `./drill/run-drill.sh broken` and `fixed` in WSL, and commit the results
- [ ] Push to GitHub and get CI green (the kind drill runs there)
