#!/usr/bin/env bash
# One command for the platform UIs and the live demos (run after platform/up.sh).
#
#   bash platform/ui.sh            start all port-forwards, print logins, open the browser
#   bash platform/ui.sh stop       stop all port-forwards
#   bash platform/ui.sh selfheal   scale the guard to 0 and watch Argo CD put it back
#   bash platform/ui.sh page       pause GitOps, take the guard down, wait for the page in
#                                  Alertmanager, then let GitOps restore it
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CTX=kind-guard-platform; K="kubectl --context $CTX"
PIDS="$ROOT/platform/.ui-pids"

# name|namespace|service|local:remote|url
FORWARDS="Grafana|monitoring|kps-grafana|3000:80|http://localhost:3000/d/admission-guard
Argo CD|argocd|argocd-server|8081:80|http://localhost:8081
Prometheus|monitoring|kps-prometheus|9090:9090|http://localhost:9090/alerts?search=AdmissionGuard
Alertmanager|monitoring|kps-alertmanager|9093:9093|http://localhost:9093"

open_url() {   # WSL -> Windows browser; plain Linux -> xdg-open; otherwise just print
  if command -v cmd.exe >/dev/null 2>&1; then cmd.exe /c start "" "$1" >/dev/null 2>&1
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1; fi
}

stop() {
  [ -f "$PIDS" ] && xargs -r kill 2>/dev/null < "$PIDS"; rm -f "$PIDS"
  pkill -f "kubectl --context $CTX .*port-forward" 2>/dev/null
  echo "port-forwards stopped"
}

start() {
  $K get ns monitoring >/dev/null 2>&1 || { echo "cluster not found: run  bash platform/up.sh  first"; exit 1; }
  stop >/dev/null
  while IFS='|' read -r name ns svc ports url; do
    local_port=${ports%%:*}
    if ss -ltn 2>/dev/null | grep -q ":$local_port "; then
      echo "port $local_port is busy (another port-forward?). Free it:  sudo ss -ltnp | grep :$local_port"; continue
    fi
    nohup $K -n "$ns" port-forward "svc/$svc" "$ports" >/dev/null 2>&1 &
    echo $! >> "$PIDS"
  done <<< "$FORWARDS"
  sleep 3
  ARGO_PW=$($K -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' 2>/dev/null | base64 -d)
  echo
  echo "  Grafana       http://localhost:3000   admin / admission-guard-demo   (dashboard: Admission Guard)"
  echo "  Argo CD       http://localhost:8081   admin / $ARGO_PW"
  echo "  Prometheus    http://localhost:9090   (Alerts: AdmissionGuard*)"
  echo "  Alertmanager  http://localhost:9093"
  echo
  echo "  Demos:  bash platform/ui.sh selfheal   |   bash platform/ui.sh page"
  echo "  Stop:   bash platform/ui.sh stop"
  while IFS='|' read -r _ _ _ _ url; do open_url "$url"; done <<< "$FORWARDS"
}

ready() { $K -n admission-guard get deploy admission-guard -o jsonpath='{.status.readyReplicas}'; }

selfheal() {
  echo "Scaling the guard to 0 by hand (watch Grafana 'Replicas up' and Argo CD)..."
  $K -n admission-guard scale deploy admission-guard --replicas=0 >/dev/null
  for i in $(seq 1 30); do
    sleep 2; r=$(ready); echo "  ${i}x2s  ready replicas: ${r:-0}"
    [ "${r:-0}" = 2 ] && { echo "Argo CD restored 2 replicas (selfHeal: Git is the source of truth)"; return; }
  done
  echo "not restored yet; check the Argo CD UI"
}

page() {
  echo "1/4 pausing GitOps for admission-guard (otherwise Argo CD heals it before the alert fires)"
  $K -n argocd patch application admission-guard --type merge -p '{"spec":{"syncPolicy":{"automated":null}}}' >/dev/null
  echo "2/4 guard -> 0 replicas: pod creates outside exempt namespaces now fail"
  $K -n admission-guard scale deploy admission-guard --replicas=0 >/dev/null
  $K run outage-proof --image=nginx --dry-run=server 2>&1 | tail -1 | sed 's/^/    /'
  echo "3/4 waiting for AdmissionGuardDown in Alertmanager (1 min 'for:' + scrape/eval, ~2-3 min)"
  for i in $(seq 1 30); do
    if $K get --raw "/api/v1/namespaces/monitoring/services/kps-alertmanager:9093/proxy/api/v2/alerts" 2>/dev/null | grep -q AdmissionGuardDown; then
      echo "    PAGE: AdmissionGuardDown firing (severity=critical -> oncall). See http://localhost:9093"; break
    fi
    sleep 10; echo "    ${i}0s ..."
  done
  read -r -p "4/4 press Enter to resume GitOps and restore the guard " _
  $K -n argocd patch application admission-guard --type merge -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}' >/dev/null
  for _ in $(seq 1 30); do sleep 3; [ "$(ready)" = 2 ] && { echo "guard restored: 2 replicas; the alert resolves within a few minutes"; return; }; done
  echo "not restored yet; check the Argo CD UI"
}

case "${1:-start}" in
  start) start ;; stop) stop ;; selfheal) selfheal ;; page) page ;;
  *) echo "usage: $0 [start|stop|selfheal|page]"; exit 2 ;;
esac
