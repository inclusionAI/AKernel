"""Production profile placement and compatibility contracts; no scale claim."""
import copy
import subprocess
import unittest
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parents[1]
PROFILE = yaml.safe_load((CHART / "values-production.yaml").read_text())


def render(values, umbrella=False):
    chart = CHART.parents[1] if umbrella else CHART
    result = subprocess.run(
        ["helm", "template", "akernel", str(chart), "-n", "akernel", "-f", "-"],
        input=yaml.safe_dump(values), text=True, capture_output=True, check=True,
    )
    return {(x["kind"], x["metadata"]["name"]): x
            for x in yaml.safe_load_all(result.stdout) if x}


class ProductionCapacityTests(unittest.TestCase):
    def test_profile_keeps_single_active_coordinator_and_persistent_redis(self):
        objects = render(PROFILE)
        coordinator = objects["Deployment", "akernel-adx-coordinator"]["spec"]
        self.assertEqual(coordinator["replicas"], 1)
        self.assertEqual(coordinator["strategy"]["type"], "Recreate")
        redis = objects["StatefulSet", "akernel-adx-redis"]["spec"]
        self.assertEqual(redis["replicas"], 1)
        container = redis["template"]["spec"]["containers"][0]
        self.assertEqual(container["args"][container["args"].index("--appendonly") + 1], "yes")
        self.assertEqual(container["args"][container["args"].index("--appendfsync") + 1], "everysec")
        self.assertEqual(container["env"][0]["valueFrom"]["secretKeyRef"]["name"], "akernel-adx-redis-auth")
        claim = redis["volumeClaimTemplates"][0]["spec"]
        self.assertEqual(claim["resources"]["requests"]["storage"], "200Gi")
        self.assertEqual(claim["accessModes"], ["ReadWriteOnce"])
        self.assertNotIn("storageClassName", claim)  # operator must select qualified SSD

    def test_api_host_separation_scales_with_replica_count_and_pdb(self):
        for replicas in (4, 8):
            values = copy.deepcopy(PROFILE)
            values["adx"]["ingressApi"]["replicas"] = replicas
            objects = render(values)
            api = objects["Deployment", "akernel-adx-ingress-api"]["spec"]
            self.assertEqual(api["replicas"], replicas)
            pod = api["template"]["spec"]
            rule = pod["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][0]
            self.assertEqual(rule["topologyKey"], "kubernetes.io/hostname")
            self.assertEqual(rule["labelSelector"]["matchLabels"], api["selector"]["matchLabels"])
            self.assertEqual(pod["topologySpreadConstraints"][0]["whenUnsatisfiable"], "ScheduleAnyway")
            self.assertEqual(api["strategy"]["rollingUpdate"], {"maxSurge": 0, "maxUnavailable": 1})
            self.assertEqual(objects["PodDisruptionBudget", "akernel-adx-ingress-api"]["spec"]["minAvailable"], replicas - 1)

    def test_profile_changes_only_capacity_and_api_placement(self):
        before, after = render({}), render(PROFILE)
        allowed = {("Deployment", "akernel-adx-coordinator"),
                   ("Deployment", "akernel-adx-ingress-api"),
                   ("StatefulSet", "akernel-adx-redis"),
                   ("PodDisruptionBudget", "akernel-adx-ingress-api"),
                   ("Secret", "akernel-adx-redis-auth")}  # offline random password
        self.assertEqual(after.keys() - before.keys(), {("PodDisruptionBudget", "akernel-adx-ingress-api")})
        for key in before.keys() - allowed:
            self.assertEqual(before[key], after[key], key)
        for kind, name, role in (("Deployment", "akernel-adx-coordinator", "coordinator"),
                                 ("Deployment", "akernel-adx-ingress-api", "ingressApi"),
                                 ("StatefulSet", "akernel-adx-redis", "redis")):
            pod = after[kind, name]["spec"]["template"]["spec"]
            self.assertEqual(pod["containers"][0]["resources"], PROFILE["adx"][role]["resources"])

    def test_redis_placement_and_storage_site_overrides(self):
        values = copy.deepcopy(PROFILE)
        placement = {"nodeSelector": {"pool": "control"},
                     "tolerations": [{"key": "control", "operator": "Exists", "effect": "NoSchedule"}],
                     "affinity": {"podAntiAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": []}}}
        values["adx"]["redis"].update(placement)
        values["adx"]["redis"]["persistence"].update(storageClassName="qualified-ssd", size="300Gi")
        spec = render(values)["StatefulSet", "akernel-adx-redis"]["spec"]
        for key, value in placement.items():
            self.assertEqual(spec["template"]["spec"][key], value)
        self.assertEqual(spec["volumeClaimTemplates"][0]["spec"]["storageClassName"], "qualified-ssd")
        self.assertEqual(spec["volumeClaimTemplates"][0]["spec"]["resources"]["requests"]["storage"], "300Gi")
        baseline = render({})["StatefulSet", "akernel-adx-redis"]["spec"]["template"]["spec"]
        for key in placement:
            self.assertNotIn(key, baseline)

    def test_umbrella_uses_explicit_core_wrapper_and_retains_light_defaults(self):
        objects = render({"core": PROFILE, "monitor": {"enabled": False}}, umbrella=True)
        self.assertEqual(objects["Deployment", "akernel-adx-ingress-api"]["spec"]["replicas"], 4)
        default = render({})
        self.assertEqual(default["Deployment", "akernel-adx-ingress-api"]["spec"]["replicas"], 1)
        self.assertEqual(default["StatefulSet", "akernel-adx-redis"]["spec"]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"]["storage"], "20Gi")


if __name__ == "__main__":
    unittest.main()
