#!/usr/bin/env bash
# Delete the platform cluster and the local Terraform state for it.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
kind delete cluster --name guard-platform
rm -rf "$ROOT/platform/.kubeconfig-internal" "$ROOT"/platform/terraform/terraform.tfstate*
