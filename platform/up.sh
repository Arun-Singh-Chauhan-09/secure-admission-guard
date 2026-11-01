#!/usr/bin/env bash
# The guard on a small platform: Terraform installs Prometheus, Alertmanager, Grafana, Loki,
# cert-manager and Argo CD on kind; Argo CD deploys the guard and its alerts from Git.
# Then it checks, end to end, that the pieces work together:
#   1. the guard denies a bad pod            4. Argo CD undoes a manual change (GitOps)
#   2. Prometheus scrapes both replicas      5. guard down -> AdmissionGuardDown reaches Alertmanager
#   3. SLO rules, dashboard, and Loki logs   6. GitOps restores the guard, alert resolves
#
#   ./platform/up.sh          (about 10 minutes; needs docker, kind, kubectl; ~4 GB RAM)
#   ./platform/down.sh
# Argo CD deploys from GitHub (main), so push your changes before running this.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLUSTER=guard-platform; K="kubectl --context kind-$CLUSTER"
OUT="$ROOT/platform/results.txt"; : > "$OUT"
REV="${TARGET_REVISION:-main}"
log()  { echo "$*" | tee -a "$OUT"; }
fail() { log "FAILED: $*"; exit 1; }
# Query Prometheus / Alertmanager through the API server's service proxy (no port-forward).
prom() { $K get --raw "/api/v1/namespaces/monitoring/services/kps-prometheus:9090/proxy/api/v1/query?query=$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "$1")"; }
prom_value() { prom "$1" | python3 -c 'import sys,json;r=json.load(sys.stdin)["data"]["result"];print(r[0]["value"][1] if r else "none")'; }
am_alerts() { $K get --raw "/api/v1/namespaces/monitoring/services/kps-alertmanager:9093/proxy/api/v2/alerts"; }
wait_for() { local what="$1" secs="$2"; shift 2; for _ in $(seq 1 $((secs/10))); do if "$@" >/dev/null 2>&1; then return 0; fi; sleep 10; done; fail "$what (waited ${secs}s)"; }
app_ok() { [ "$($K -n argocd get application "$1" -o jsonpath='{.status.sync.status}/{.status.health.status}')" = "Synced/Healthy" ]; }
two_scraped() { [ "$(prom_value 'sum(up{namespace="admission-guard"})')" = 2 ]; }
alert_firing() { am_alerts | grep -q AdmissionGuardDown; }
replicas_are() { [ "$($K -n admission-guard get deploy admission-guard -o jsonpath='{.status.readyReplicas}')" = "$1" ]; }

# ---- cluster + image -------------------------------------------------------------
kind get clusters | grep -qx $CLUSTER || kind create cluster --name $CLUSTER --config - <<'KIND'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes: [{role: control-plane}, {role: worker}, {role: worker}]
KIND
docker build -q -t admission-guard:dev "$ROOT" >/dev/null
kind load docker-image admission-guard:dev --name $CLUSTER

# ---- Terraform (in a container, on kind's Docker network: works on Linux and WSL) --
kind get kubeconfig --internal --name $CLUSTER > "$ROOT/platform/.kubeconfig-internal"
TF="docker run --rm --network kind -v $ROOT:/work -w /work/platform/terraform hashicorp/terraform:1.9.8"
$TF init -input=false
$TF apply -auto-approve -input=false \
  -var kubeconfig_path=/work/platform/.kubeconfig-internal -var target_revision="$REV"

# ---- GitOps sync -------------------------------------------------------------------
log "== Argo CD syncs the guard and its observability from Git ($REV)"
wait_for "admission-guard app Synced/Healthy" 600 app_ok admission-guard
wait_for "observability app Synced/Healthy" 300 app_ok admission-guard-observability
log "admission-guard: Synced/Healthy · admission-guard-observability: Synced/Healthy"

# ---- 1. policy ---------------------------------------------------------------------
log "== 1. the guard denies an unpinned public image"
sleep 10   # CA injection
RESULT=$($K run bad --image=nginx:latest --dry-run=server 2>&1 | tail -1 || true)
log "$RESULT"
grep -q "denied the request" <<<"$RESULT" || fail "no denial from the webhook"

# ---- 2. scraping -------------------------------------------------------------------
log "== 2. Prometheus scrapes both replicas"
wait_for "2 replicas up in Prometheus" 300 two_scraped
log "sum(up{namespace=\"admission-guard\"}) = $(prom_value 'sum(up{namespace="admission-guard"})')"
log "denied decisions so far: $(prom_value 'sum(admission_guard_decisions_total{result="denied"})')"

# ---- 3. rules + dashboard ------------------------------------------------------------
log "== 3. SLO rules and dashboard loaded"
RULES=$($K get --raw "/api/v1/namespaces/monitoring/services/kps-prometheus:9090/proxy/api/v1/rules")
for r in AdmissionGuardDown AdmissionGuardDegraded AdmissionGuardErrorBudgetBurnFast AdmissionGuardErrorBudgetBurnSlow; do
  grep -q "\"$r\"" <<<"$RULES" || fail "rule $r not loaded"
done
log "alert rules loaded: AdmissionGuardDown, Degraded, ErrorBudgetBurnFast/Slow, Slow, CertExpiringSoon"
$K -n monitoring get configmap admission-guard-dashboard >/dev/null || fail "dashboard ConfigMap missing"
log "Grafana dashboard ConfigMap: present (sidecar loads it as 'Admission Guard')"

log "== 3b. Loki has the guard's decision logs (shipped by Alloy)"
loki_hits() { $K get --raw "/api/v1/namespaces/monitoring/services/loki-gateway:80/proxy/loki/api/v1/query_range?query=$(python3 -c 'import urllib.parse;print(urllib.parse.quote("{namespace=\"admission-guard\"} |= \"policy_violation\""))')&limit=5" \
  | python3 -c 'import sys,json;print(sum(len(s["values"]) for s in json.load(sys.stdin)["data"]["result"]))'; }
has_logs() { [ "$(loki_hits)" -gt 0 ]; }
wait_for "policy_violation log lines in Loki" 240 has_logs
log "Loki: $(loki_hits) policy_violation line(s) for the denied pod, queryable in Grafana"

# ---- 4. GitOps self-heal -------------------------------------------------------------
log "== 4. someone scales the guard to 1 by hand; Argo CD puts it back"
$K -n admission-guard scale deploy admission-guard --replicas=1 >/dev/null
wait_for "Argo CD restoring 2 replicas" 180 replicas_are 2
log "replicas back to 2 (selfHeal: Git is the source of truth)"

# ---- 5. outage -> alert ----------------------------------------------------------------
log "== 5. outage: pause GitOps, scale the guard to 0, wait for the page"
$K -n argocd patch application admission-guard --type merge -p '{"spec":{"syncPolicy":{"automated":null}}}' >/dev/null
$K -n admission-guard scale deploy admission-guard --replicas=0 >/dev/null
wait_for "AdmissionGuardDown in Alertmanager" 360 alert_firing
log "Alertmanager: AdmissionGuardDown firing, severity=critical -> receiver oncall"

# ---- 6. recovery ---------------------------------------------------------------------
log "== 6. resume GitOps; Argo CD restores the guard"
$K -n argocd patch application admission-guard --type merge -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}' >/dev/null
wait_for "guard restored to 2 replicas" 300 replicas_are 2
log "replicas: 2 · alert resolves on the next evaluations"
log "== platform drill passed"
echo "saved to $OUT"
$TF output -raw next_steps 2>/dev/null || true
