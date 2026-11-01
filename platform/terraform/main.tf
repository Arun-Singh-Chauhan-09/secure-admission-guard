# The platform under the guard: metrics, logs, certificates, and GitOps.
# Terraform installs the platform once; from then on Argo CD deploys the guard and its
# alerts/dashboards from Git, so a change goes through a commit, not kubectl.
#
# Order matters: the monitoring CRDs (PodMonitor, PrometheusRule) and cert-manager's CRDs
# must exist before Argo CD syncs manifests that use them.

resource "helm_release" "kube_prometheus_stack" {
  name             = "kps"
  repository       = "https://prometheus-community.github.io/helm-charts"
  chart            = "kube-prometheus-stack"
  version          = var.chart_versions.kube_prometheus_stack
  namespace        = "monitoring"
  create_namespace = true
  timeout          = 900
  values           = [file("${path.module}/values/kube-prometheus-stack.yaml")]
}

# Logs: Loki stores them, Alloy ships every pod's logs to it (read through the Kubernetes
# API, so no hostPath mounts). The guard logs one JSON line per policy violation.
resource "helm_release" "loki" {
  name             = "loki"
  repository       = "https://grafana.github.io/helm-charts"
  chart            = "loki"
  version          = var.chart_versions.loki
  namespace        = "monitoring"
  create_namespace = true
  timeout          = 600
  values           = [file("${path.module}/values/loki.yaml")]

  depends_on = [helm_release.kube_prometheus_stack] # same namespace; one at a time
}

resource "helm_release" "alloy" {
  name       = "alloy"
  repository = "https://grafana.github.io/helm-charts"
  chart      = "alloy"
  version    = var.chart_versions.alloy
  namespace  = "monitoring"
  timeout    = 300

  values = [file("${path.module}/values/alloy.yaml")]

  depends_on = [helm_release.loki]
}

resource "helm_release" "cert_manager" {
  name             = "cert-manager"
  repository       = "https://charts.jetstack.io"
  chart            = "cert-manager"
  version          = var.chart_versions.cert_manager
  namespace        = "cert-manager"
  create_namespace = true
  timeout          = 600

  set {
    name  = "crds.enabled"
    value = "true"
  }
  # Expose certificate expiry to Prometheus (AdmissionGuardCertExpiringSoon).
  set {
    name  = "prometheus.servicemonitor.enabled"
    value = "true"
  }

  depends_on = [helm_release.kube_prometheus_stack] # ServiceMonitor CRD
}

resource "helm_release" "argo_cd" {
  name             = "argocd"
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-cd"
  version          = var.chart_versions.argo_cd
  namespace        = "argocd"
  create_namespace = true
  timeout          = 600
  values           = [file("${path.module}/values/argo-cd.yaml")]
}

# The Argo CD Applications themselves, as a Helm release so Terraform doesn't need the
# Application CRD at plan time.
resource "helm_release" "argocd_apps" {
  name       = "argocd-apps"
  repository = "https://argoproj.github.io/argo-helm"
  chart      = "argocd-apps"
  version    = var.chart_versions.argocd_apps
  namespace  = "argocd"

  values = [templatefile("${path.module}/values/argocd-apps.yaml.tftpl", {
    repo_url        = var.repo_url
    target_revision = var.target_revision
  })]

  depends_on = [
    helm_release.argo_cd,
    helm_release.cert_manager,
    helm_release.kube_prometheus_stack,
    helm_release.alloy,
  ]
}
