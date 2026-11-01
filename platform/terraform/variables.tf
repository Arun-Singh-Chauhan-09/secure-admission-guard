variable "kubeconfig_path" {
  description = "Kubeconfig of the target cluster."
  type        = string
  default     = "~/.kube/config"
}

variable "kube_context" {
  description = "Context to deploy into. Defaults to the kind cluster from platform/up.sh."
  type        = string
  default     = "kind-guard-platform"
}

variable "repo_url" {
  description = "Git repository Argo CD deploys from."
  type        = string
  default     = "https://github.com/Arun-Singh-Chauhan-09/secure-admission-guard.git"
}

variable "target_revision" {
  description = "Branch, tag or commit Argo CD tracks."
  type        = string
  default     = "main"
}

# Chart versions: "~x.y.0" takes the newest patch of that minor release.
variable "chart_versions" {
  type = object({
    kube_prometheus_stack = string
    cert_manager          = string
    argo_cd               = string
    argocd_apps           = string
  })

  default = {
    kube_prometheus_stack = "~65.1.0"
    cert_manager          = "~1.16.0"
    argo_cd               = "~7.6.0"
    argocd_apps           = "~2.0.0"
  }
}
