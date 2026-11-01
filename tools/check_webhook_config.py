#!/usr/bin/env python3
"""Fail CI if a fail-closed admission webhook could lock the cluster.

  python3 tools/check_webhook_config.py deploy/fixed

For every webhook with failurePolicy: Fail:
  W1  namespaceSelector excludes the webhook's own namespace and kube-system,
      via kubernetes.io/metadata.name NotIn (a label teams cannot fake)
  W2  timeoutSeconds <= 5
  W3  the backing Deployment has >= 2 replicas
  W4  a PodDisruptionBudget selects the backing pods
  W5  sideEffects: None (so dry-run works and retries are safe)
"""
import sys
from pathlib import Path

import yaml


def load(path):
    p = Path(path)
    files = [p] if p.is_file() else sorted(p.rglob("*.y*ml"))
    return [d for f in files for d in yaml.safe_load_all(f.read_text()) if isinstance(d, dict)]


def excluded_namespaces(webhook):
    sel = webhook.get("namespaceSelector") or {}
    out = set()
    for e in sel.get("matchExpressions", []):
        if e.get("key") == "kubernetes.io/metadata.name" and e.get("operator") == "NotIn":
            out |= set(e.get("values", []))
    return out


def check(docs):
    errs = []
    deployments = {(d["metadata"].get("namespace"), d["metadata"]["name"]): d for d in docs if d["kind"] == "Deployment"}
    services = {(d["metadata"].get("namespace"), d["metadata"]["name"]): d for d in docs if d["kind"] == "Service"}
    pdbs = [d for d in docs if d["kind"] == "PodDisruptionBudget"]

    for cfg in (d for d in docs if d["kind"] == "ValidatingWebhookConfiguration"):
        for w in cfg.get("webhooks", []):
            name = w["name"]
            svc_ref = w.get("clientConfig", {}).get("service", {})
            ns = svc_ref.get("namespace")
            if w.get("sideEffects") != "None":
                errs.append(f"{name}: W5 sideEffects must be None")
            if w.get("failurePolicy", "Fail") != "Fail":
                continue  # fail-open webhooks cannot deadlock (but cannot enforce either)
            missing = {ns, "kube-system"} - excluded_namespaces(w)
            if missing:
                errs.append(f"{name}: W1 namespaceSelector must exclude {sorted(missing)}: the webhook would block its own restart")
            if w.get("timeoutSeconds", 10) > 5:
                errs.append(f"{name}: W2 timeoutSeconds {w.get('timeoutSeconds', '10 (default)')} > 5")
            svc = services.get((ns, svc_ref.get("name")))
            selector = (svc or {}).get("spec", {}).get("selector", {})
            backing = [d for (dns, _), d in deployments.items() if dns == ns and
                       all(d["spec"]["template"]["metadata"]["labels"].get(k) == v for k, v in selector.items())]
            if not backing:
                errs.append(f"{name}: backing Deployment not found in these manifests")
                continue
            for d in backing:
                if d["spec"].get("replicas", 1) < 2:
                    errs.append(f"{name}: W3 Deployment {d['metadata']['name']} has {d['spec'].get('replicas', 1)} replica(s)")
                labels = d["spec"]["template"]["metadata"]["labels"]
                if not any(p["metadata"].get("namespace") == ns and
                           all(labels.get(k) == v for k, v in p["spec"]["selector"].get("matchLabels", {}).items())
                           for p in pdbs):
                    errs.append(f"{name}: W4 no PodDisruptionBudget for {d['metadata']['name']}")
    return errs


def main(argv):
    errs = check(load(argv[0] if argv else "deploy/fixed"))
    for e in errs:
        print("FAIL", e)
    print("OK webhook config" if not errs else f"{len(errs)} problem(s)")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
