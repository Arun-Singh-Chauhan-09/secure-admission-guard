"""Decide whether a Pod may run. Pure functions: no I/O, easy to test.

Rules (all are checked, so the developer sees every violation at once):
  - images come from an allowed registry prefix (e.g. "registry.internal/")
  - images are pinned by digest (@sha256:...), not by a tag that can be moved
  - no host namespaces (hostNetwork / hostPID / hostIPC) and no hostPath volumes
  - no privileged containers, no allowPrivilegeEscalation, no added capabilities
  - every container runs as non-root (pod-level runAsNonRoot counts for all)
"""
import re
from dataclasses import dataclass, field

DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


@dataclass
class Config:
    allowed_registries: list = field(default_factory=lambda: ["registry.internal/"])
    require_digest: bool = True


def _containers(spec):
    """Every container of the pod: init, normal and ephemeral (debug) containers."""
    for key in ("initContainers", "containers", "ephemeralContainers"):
        for c in spec.get(key) or []:
            yield c


def _check_image(c, cfg):
    image, name, problems = c.get("image", ""), c.get("name", "?"), []
    if cfg.allowed_registries and not any(image.startswith(p) for p in cfg.allowed_registries):
        problems.append(f'container "{name}": image "{image}" is not from an allowed registry {cfg.allowed_registries}')
    if cfg.require_digest and not DIGEST.search(image):
        problems.append(f'container "{name}": image "{image}" must be pinned by digest (@sha256:...)')
    return problems


def evaluate(pod, cfg):
    """Return the list of violations for a Pod (a dict as the API server sends it). Empty = allowed."""
    spec = pod.get("spec") or {}
    v = []
    if spec.get("hostNetwork") or spec.get("hostPID") or spec.get("hostIPC"):
        v.append("host namespaces (hostNetwork/hostPID/hostIPC) are not allowed")
    for vol in spec.get("volumes") or []:
        if "hostPath" in vol:
            v.append(f'volume "{vol.get("name")}": hostPath is not allowed')

    pod_non_root = (spec.get("securityContext") or {}).get("runAsNonRoot") is True
    for c in _containers(spec):
        name = c.get("name", "?")
        sc = c.get("securityContext") or {}
        v += _check_image(c, cfg)
        if sc.get("privileged") is True:
            v.append(f'container "{name}": privileged is not allowed')
        # Kubernetes allows escalation unless it is explicitly set to false.
        if sc.get("allowPrivilegeEscalation") is not False:
            v.append(f'container "{name}": set allowPrivilegeEscalation: false')
        if not (pod_non_root or sc.get("runAsNonRoot") is True):
            v.append(f'container "{name}": must run as non-root (runAsNonRoot: true)')
        added = (sc.get("capabilities") or {}).get("add") or []
        if added:
            v.append(f'container "{name}": adding capabilities {added} is not allowed')
    return v
