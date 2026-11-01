"""Run from the repo root: python3 -m unittest discover -s tests -v"""
import copy
import unittest

from guard.policy import Config, evaluate

DIGEST = "@sha256:" + "0123456789abcdef" * 4
CFG = Config(allowed_registries=["registry.internal/"], require_digest=True)

GOOD = {"metadata": {"name": "ok"}, "spec": {
    "securityContext": {"runAsNonRoot": True},
    "containers": [{"name": "app", "image": "registry.internal/team/app" + DIGEST,
                    "securityContext": {"allowPrivilegeEscalation": False}}]}}


def pod(**changes):
    p = copy.deepcopy(GOOD)
    c = p["spec"]["containers"][0]
    for k, v in changes.items():
        c[k] = v
    return p


class PolicyTests(unittest.TestCase):
    def test_compliant_pod_is_allowed(self):
        self.assertEqual(evaluate(GOOD, CFG), [])

    def test_image_must_come_from_internal_registry(self):
        v = evaluate(pod(image="docker.io/library/nginx" + DIGEST), CFG)
        self.assertEqual(len(v), 1)
        self.assertIn("allowed registry", v[0])

    def test_tag_without_digest_rejected(self):
        v = evaluate(pod(image="registry.internal/team/app:1.2.3"), CFG)
        self.assertEqual(len(v), 1)
        self.assertIn("digest", v[0])

    def test_lookalike_registry_prefix_rejected(self):
        # "registry.internal.evil.com/" must not pass a prefix check for "registry.internal/"
        self.assertTrue(evaluate(pod(image="registry.internal.evil.com/app" + DIGEST), CFG))

    def test_init_and_ephemeral_containers_are_checked(self):
        p = {"spec": {"securityContext": {"runAsNonRoot": True},
             "initContainers": [{"name": "init", "image": "busybox:1.36", "securityContext": {"allowPrivilegeEscalation": False}}],
             "ephemeralContainers": [{"name": "debug", "image": "alpine", "securityContext": {"allowPrivilegeEscalation": False}}]}}
        v = "\n".join(evaluate(p, CFG))
        self.assertIn('"init"', v)
        self.assertIn('"debug"', v)

    def test_privileged_host_access_and_capabilities_rejected(self):
        p = {"spec": {"hostNetwork": True, "volumes": [{"name": "root", "hostPath": {"path": "/"}}],
             "containers": [{"name": "x", "image": "registry.internal/x" + DIGEST,
                             "securityContext": {"privileged": True, "capabilities": {"add": ["SYS_ADMIN"]}}}]}}
        v = "\n".join(evaluate(p, CFG))
        for want in ("host namespaces", "hostPath", "privileged", "allowPrivilegeEscalation", "non-root", "SYS_ADMIN"):
            self.assertIn(want, v)

    def test_all_violations_reported_at_once(self):
        self.assertGreaterEqual(len(evaluate({"spec": {"containers": [{"name": "x", "image": "nginx"}]}}, CFG)), 4)

    def test_container_level_non_root_is_enough(self):
        p = pod(securityContext={"allowPrivilegeEscalation": False, "runAsNonRoot": True})
        del p["spec"]["securityContext"]
        self.assertEqual(evaluate(p, CFG), [])


if __name__ == "__main__":
    unittest.main()
