output "next_steps" {
  value = <<-EOT
    Argo CD UI:   kubectl --context ${var.kube_context} -n argocd port-forward svc/argocd-server 8081:80
                  user admin, password: kubectl --context ${var.kube_context} -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d
    Grafana:      kubectl --context ${var.kube_context} -n monitoring port-forward svc/kps-grafana 3000:80   (admin / see values file)
    Prometheus:   kubectl --context ${var.kube_context} -n monitoring port-forward svc/kps-prometheus 9090:9090
    Alertmanager: kubectl --context ${var.kube_context} -n monitoring port-forward svc/kps-alertmanager 9093:9093
  EOT
}
