#!/usr/bin/env bash
# The fail-closed webhook outage, reproduced on kind.
#   ./drill/run-drill.sh broken    # node drain -> nothing can create pods any more
#   ./drill/run-drill.sh fixed     # same drain -> guard recovers, policy still enforced
#   ./drill/run-drill.sh cleanup
# Needs: docker, kind, kubectl, internet (cert-manager install).
set -uo pipefail
MODE="${1:-}"; ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLUSTER=guard-drill; K="kubectl --context kind-$CLUSTER"
OUT="$ROOT/drill/results-$MODE.txt"
log() { echo "$*" | tee -a "$OUT"; }

case "$MODE" in broken|fixed) ;; cleanup) kind delete cluster --name $CLUSTER; exit 0 ;;
  *) echo "usage: $0 broken|fixed|cleanup"; exit 2 ;; esac
: > "$OUT"

kind get clusters | grep -qx $CLUSTER || kind create cluster --name $CLUSTER --config - <<'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes: [{role: control-plane}, {role: worker}, {role: worker}, {role: worker}]
EOF
$K apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.16.2/cert-manager.yaml >/dev/null
$K -n cert-manager rollout status deploy/cert-manager-webhook --timeout=180s >/dev/null
docker build -q -t admission-guard:dev "$ROOT" >/dev/null && kind load docker-image admission-guard:dev --name $CLUSTER

$K delete validatingwebhookconfiguration admission-guard --ignore-not-found >/dev/null
$K apply -f "$ROOT/deploy/$MODE/" >/dev/null
$K -n admission-guard rollout status deploy/admission-guard --timeout=180s >/dev/null
sleep 5   # CA injection

log "== [$MODE] policy works: unpinned public image is denied"
$K run bad --image=nginx:latest --dry-run=server 2>&1 | tail -1 | tee -a "$OUT"

log "== [$MODE] drain the node running a guard replica (routine OS patching)"
NODE=$($K -n admission-guard get pod -l app.kubernetes.io/name=admission-guard -o jsonpath='{.items[0].spec.nodeName}')
timeout 120 $K drain "$NODE" --ignore-daemonsets --delete-emptydir-data --timeout=90s >/dev/null 2>&1
sleep 15
log "guard pods running: $($K -n admission-guard get pods --no-headers 2>/dev/null | grep -c Running)"
log "guard ReplicaSet events:"
$K -n admission-guard get events --field-selector reason=FailedCreate -o custom-columns=MSG:.message --no-headers 2>/dev/null | head -2 | tee -a "$OUT"

log "== [$MODE] can kube-system still create pods? (restart CoreDNS)"
$K -n kube-system rollout restart deploy/coredns >/dev/null
if $K -n kube-system rollout status deploy/coredns --timeout=60s >/dev/null 2>&1; then
  log "CoreDNS restarted: OK"
else
  log "CoreDNS stuck: cluster DNS is degrading"
fi
$K uncordon "$NODE" >/dev/null

if [ "$MODE" = broken ]; then
  log "== break-glass recovery"
  $K delete validatingwebhookconfiguration admission-guard | tee -a "$OUT"
fi
echo "saved to $OUT"
