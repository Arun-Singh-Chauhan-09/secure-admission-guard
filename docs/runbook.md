# Runbook: admission-guard

The guard is a **fail-closed** validating webhook for pods. When it can't answer, the API
server rejects pod creates in every namespace except the exempt ones (`kube-system`,
`admission-guard`, `cert-manager`, and on the platform `monitoring` and `argocd`).

## AdmissionGuardDown
**Impact:** no new pods anywhere outside the exempt namespaces: deploys, scale-ups,
restarts and Jobs all fail.
1. `kubectl -n admission-guard get pods,events` — crashloop, pending (no nodes), image?
2. It can always restart itself: its namespace is exempt. If pods are Pending, look at
   nodes (`kubectl get nodes`), the PDB, and anti-affinity (needs 2 schedulable nodes).
3. On the platform, check Argo CD: `kubectl -n argocd get application admission-guard`.
   A paused or failed sync can leave it at 0 replicas.
4. **Break-glass**, only if it can't be restored quickly and the outage is worse than
   running without the policy for a while:
   `kubectl delete validatingwebhookconfiguration admission-guard`.
   Security is OFF until it's back. Tell the security team; Argo CD recreates it on the
   next sync. Write a post-incident review.

## AdmissionGuardDegraded
One replica left. Not an outage yet, but the next drain or crash is one.
Find out why the other is down (`kubectl -n admission-guard describe pod`), and hold any
node maintenance until both are back.

## ErrorBudgetBurn
Webhook calls are failing (timeouts, TLS, connection resets) faster than the 99.9 % SLO
allows. Fast burn pages; slow burn is a ticket.
1. Dashboard "Admission Guard": API server → webhook calls panel, call errors line.
2. Guard logs: `kubectl -n admission-guard logs deploy/admission-guard | grep connection_error`.
3. TLS: is the CA in the webhook config current? (`cert-manager.io/inject-ca-from`)
4. Network: the NetworkPolicy must allow 8443 from the API server.

## AdmissionGuardSlow
Average decision time over 100 ms (the API server gives up at 3 s). Check CPU throttling,
node pressure, and request rate on the dashboard.

## AdmissionGuardCertExpiringSoon
cert-manager should renew 10 days before expiry. Check
`kubectl -n admission-guard describe certificate admission-guard` and the cert-manager logs.
The guard reloads a renewed certificate without a restart.
