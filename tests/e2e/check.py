"""Scenarios for the reconcile Job, called by tests/e2e/run.sh.

Each scenario renders the chart, applies its RBAC and Secrets to the test API server, runs
files/reconcile.py with a token of the rendered ServiceAccount and checks Stalwart, the
StatefulSet and the Secret afterwards. Stalwart: http://127.0.0.1:8080 (recovery admin
admin:test-admin), SMTP AUTH on 127.0.0.1:2525. The API server runs controllers but no pods,
so the stand-in StatefulSet has zero replicas.
"""
import base64
import json
import os
import smtplib
import subprocess
import sys
import time
import urllib.request

import yaml

CHART, WORK, CLI_IMAGE = os.environ["CHART"], os.environ["WORK"], os.environ["CLI_IMAGE"]
SCRIPT = os.path.join(CHART, "files", "reconcile.py")
PREFIX = "stalwart.s7n.systems/"
failures = []
LAST_OUTPUT = [""]


def check(name, condition, detail=""):
    print(f"{'ok  ' if condition else 'FAIL'} {name}{'' if condition else ': ' + str(detail)[:400]}", flush=True)
    if not condition:
        failures.append(name)


def kubectl(*args, stdin=None, check_rc=True):
    result = subprocess.run(["kubectl", *args], input=stdin, capture_output=True, text=True)
    if check_rc and result.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {result.stderr}")
    return result.stdout


def jmap(method, args):
    body = json.dumps({"using": ["urn:ietf:params:jmap:core", "urn:stalwart:jmap"], "methodCalls": [[method, args, "c"]]}).encode()
    req = urllib.request.Request("http://127.0.0.1:8080/jmap/", data=body, method="POST")
    req.add_header("Authorization", "Basic " + base64.b64encode(b"admin:test-admin").decode())
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["methodResponses"][0][1]


def account_id(name):
    return jmap("x:Account/query", {"filter": {"name": name}})["ids"][0]


def app_passwords(name):
    return {p["id"]: p for p in jmap("x:AppPassword/get", {"accountId": account_id(name)})["list"]}


def managed(name):
    """References of the managed app passwords (Stalwart reuses ids, so these identify them)."""
    return {p["description"].rsplit("#", 1)[1] for p in app_passwords(name).values() if "#" in (p.get("description") or "")}


def destroy(name, reference):
    ids = [i for i, p in app_passwords(name).items() if (p.get("description") or "").endswith("#" + reference)]
    jmap("x:AppPassword/set", {"accountId": account_id(name), "destroy": ids})


def smtp_login(address, password):
    try:
        with smtplib.SMTP("127.0.0.1", 2525, timeout=10) as smtp:
            smtp.ehlo("test.example.org")
            smtp.login(address, password)
        return True
    except (smtplib.SMTPException, OSError):
        return False


def secret(name, namespace="mail"):
    raw = json.loads(kubectl("-n", namespace, "get", "secret", name, "-o", "json"))
    data = {k: base64.b64decode(v).decode() for k, v in (raw.get("data") or {}).items()}
    annotations = {k[len(PREFIX):]: v for k, v in raw["metadata"].get("annotations", {}).items() if k.startswith(PREFIX)}
    return data, annotations


def annotate(name, *pairs, namespace="mail"):
    kubectl("-n", namespace, "annotate", "secret", name, "--overwrite", *[f"{PREFIX}{p}" for p in pairs])


def render(values):
    path = os.path.join(WORK, "values.yaml")
    with open(path, "w") as f:
        yaml.safe_dump(values, f)
    out = subprocess.run(["helm", "template", "stalwart", CHART, "-n", "mail", "-f", path], capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


BASE = {
    "dataStore": {"backend": "rocksdb"},
    "blobStore": {"backend": "existing"},
    "logging": {"console": {"enabled": False}},
    "provisioning": {"existingSecret": "admin"},
    "domain": "example.org",
    "hostname": "mail.example.org",
}


def deploy(accounts, smtp_check=True, extra=None):
    """Renders the chart, applies the accounts plan to Stalwart and the RBAC and Secrets to the
    API server; returns the Job's config with the local Stalwart URL."""
    values = {**BASE, **(extra or {}), "systemAccounts": {"accounts": accounts, "smtpCheck": {"enabled": smtp_check, "host": "127.0.0.1", "port": 2525}}}
    docs = render(values)
    plan = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "stalwart-provisioning")["data"]["plan.ndjson"]
    with open(os.path.join(WORK, "plan.ndjson"), "w") as f:
        f.write(plan)
    subprocess.run(["docker", "run", "--rm", "--network", "host", "-v", f"{WORK}:/plan:ro", "-e", "STALWART_URL=http://127.0.0.1:8080",
                    "-e", "STALWART_USER=admin", "-e", "STALWART_PASSWORD=test-admin", "--entrypoint", "/usr/local/bin/stalwart-cli",
                    CLI_IMAGE, "apply", "--file", "/plan/plan.ndjson", "--quiet"], check=True, capture_output=True)
    for doc in docs:
        if doc["kind"] in ("ServiceAccount", "Role", "RoleBinding") or (doc["kind"] == "Secret" and doc["metadata"].get("labels", {}).get("app.kubernetes.io/component") == "system-accounts"):
            kubectl("apply", "-f", "-", stdin=yaml.safe_dump(doc))
    configmap = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "stalwart-reconcile")
    config = json.loads(configmap["data"]["config.json"])
    config["stalwartUrl"] = "http://127.0.0.1:8080"
    return config


def run_job(config, job_uid="job-1"):
    with open(os.path.join(WORK, "config.json"), "w") as f:
        json.dump(config, f)
    token = kubectl("-n", "mail", "create", "token", "stalwart-reconcile", "--duration", "10m").strip()
    with open(os.path.join(WORK, "token"), "w") as f:
        f.write(token)
    kubeconfig = yaml.safe_load(open(os.environ["KUBECONFIG"]))
    cluster = kubeconfig["clusters"][0]["cluster"]
    with open(os.path.join(WORK, "ca.crt"), "wb") as f:
        f.write(base64.b64decode(cluster["certificate-authority-data"]))
    env = {**os.environ, "CONFIG_FILE": os.path.join(WORK, "config.json"), "KUBERNETES_API": cluster["server"],
           "KUBERNETES_TOKEN_FILE": os.path.join(WORK, "token"), "KUBERNETES_CA_FILE": os.path.join(WORK, "ca.crt"),
           "STALWART_USER": "admin", "STALWART_PASSWORD": "test-admin", "JOB_UID": job_uid}
    result = subprocess.run([sys.executable, "-I", SCRIPT], env=env, capture_output=True, text=True, timeout=300)
    LAST_OUTPUT[0] = result.stdout + result.stderr
    for line in (result.stdout + result.stderr).strip().splitlines():
        print(f"       | {line}")
    return result.returncode


def statefulset():
    """(generation, settings hash on the pod template)"""
    raw = json.loads(kubectl("-n", "mail", "get", "statefulset", "stalwart", "-o", "json"))
    stamp = raw["spec"]["template"]["metadata"].get("annotations", {}).get(PREFIX + "settings") or ""
    return raw["metadata"]["generation"], stamp.split("@")[0]


def marker_role():
    """(role id, state, stamp) of the settings marker in Stalwart's database, or Nones."""
    for role in jmap("x:Role/get", {"properties": ["description"]})["list"]:
        if (role.get("description") or "").startswith("managed:stalwart/settings "):
            state, stamp = role["description"].split(" ")[1:3]
            return role["id"], state, stamp
    return None, None, None


def marker():
    """The settings hash of a completed rollout recorded in Stalwart's database, or None."""
    _, state, stamp = marker_role()
    return stamp.split("@")[0] if state == "done" else None


def set_marker(state, stamp):
    role_id, _, _ = marker_role()
    jmap("x:Role/set", {"update": {role_id: {"description": f"managed:stalwart/settings {state} {stamp}"}}})


def stamp_of_statefulset():
    raw = json.loads(kubectl("-n", "mail", "get", "statefulset", "stalwart", "-o", "json"))
    return raw["spec"]["template"]["metadata"]["annotations"][PREFIX + "settings"]


def reset_stalwart():
    """A real database reset: Stalwart restarted on an empty database (run.sh's container)."""
    subprocess.run(["bash", os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.sh"), "--reset-stalwart"], check=True)


# Stand-in for the release's StatefulSet: zero replicas, so its rollouts complete without pods.
kubectl("apply", "-f", "-", stdin=yaml.safe_dump({
    "apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": {"name": "stalwart", "namespace": "mail"},
    "spec": {"replicas": 0, "serviceName": "stalwart", "selector": {"matchLabels": {"app": "stalwart"}},
             "template": {"metadata": {"labels": {"app": "stalwart"}}, "spec": {"containers": [{"name": "c", "image": "busybox"}]}}}}))

SYSTEM = {
    "id": "system", "name": "system", "aliases": ["no-reply"],
    "appPassword": {"allowedIps": ["127.0.0.0/8", "::1/128"], "permissions": ["authenticate", "emailSend"]},
    "secret": {"name": "mailer-smtp", "keys": ["password", "OPENPROJECT_SMTP__PASSWORD"]},
}
ADDRESS = "system@example.org"

print("-- fresh installation: restart for the new settings, then the account")
config = deploy([SYSTEM])
generation, _ = statefulset()
check("job succeeds", run_job(config) == 0)
generation2, applied = statefulset()
check("StatefulSet restarted once with the settings hash", applied == config["restart"]["settingsHash"] and generation2 == generation + 1, (generation, generation2, applied))
check("settings hash recorded in Stalwart's database", marker() == config["restart"]["settingsHash"], marker())
data, ann = secret("mailer-smtp")
first = ann.get("credential")
check("Secret holds the password under both keys", data.get("password") and data["password"] == data.get("OPENPROJECT_SMTP__PASSWORD"), data.keys())
check("credential recorded, unlocked", first and "lock" not in ann, ann)
check("one managed app password in Stalwart", managed("system") == {first}, app_passwords("system"))
check("SMTP AUTH works with the published password", smtp_login(ADDRESS, data["password"]))

print("-- RBAC: the token reaches only what the chart names")
subject = "system:serviceaccount:mail:stalwart-reconcile"
for verb, resource, ns, expected in [("get", "secrets/mailer-smtp", "mail", "yes"), ("update", "secrets/mailer-smtp", "mail", "yes"),
                                     ("get", "secrets/admin", "mail", "no"), ("list", "secrets", "mail", "no"),
                                     ("create", "secrets", "mail", "no"), ("patch", "statefulsets/stalwart", "mail", "yes"),
                                     ("patch", "statefulsets/other", "mail", "no"), ("patch", "deployments/web", "mail", "no"),
                                     ("get", "secrets/mailer-smtp", "apps", "no")]:
    answer = kubectl("auth", "can-i", verb, resource, "-n", ns, "--as", subject, check_rc=False).strip()
    check(f"can-i {verb} {resource} in {ns}: {expected}", answer == expected, answer)

print("-- unchanged upgrade")
check("job succeeds", run_job(config) == 0)
data2, ann2 = secret("mailer-smtp")
check("no restart", statefulset()[0] == generation2)
check("same credential, same Secret data", ann2.get("credential") == first and data2 == data, (ann2, data2 == data))

print("-- changed settings: one restart")
changed = deploy([SYSTEM], extra={"provisioning": {"existingSecret": "admin", "extraPlan": [{"@type": "update", "object": "Http", "value": {"clientIpSource": "xForwardedFor"}}]}})
check("settings hash differs", changed["restart"]["settingsHash"] != config["restart"]["settingsHash"])
check("job succeeds", run_job(changed) == 0)
check("restarted with the new hash", statefulset() == (generation2 + 1, changed["restart"]["settingsHash"]), statefulset())
check("marker updated", marker() == changed["restart"]["settingsHash"])
check("job again: no further restart", run_job(changed) == 0 and statefulset()[0] == generation2 + 1)
config = changed

print("-- interrupted restart: resumed, not repeated")
generation_now = statefulset()[0]
current_stamp = stamp_of_statefulset()
set_marker("pending", current_stamp)  # the run died after the rollout, before marking it done
check("job succeeds", run_job(config) == 0)
check("no second rollout, marker done", statefulset()[0] == generation_now and marker_role()[1:] == ("done", current_stamp), (statefulset(), marker_role()))
earlier = f"{config['restart']['settingsHash']}@{int(time.time()) - 60}"
set_marker("pending", earlier)        # the run died after recording, before patching
check("job succeeds", run_job(config) == 0)
check("exactly one patch, with the recorded stamp", statefulset()[0] == generation_now + 1 and stamp_of_statefulset() == earlier
      and marker_role()[1:] == ("done", earlier), (statefulset(), stamp_of_statefulset(), marker_role()))
check("job again: nothing to do", run_job(config) == 0 and statefulset()[0] == generation_now + 1)

print("-- narrowed and widened app password restrictions")
reference = secret("mailer-smtp")[1]["credential"]
password = secret("mailer-smtp")[0]["password"]
narrowed = deploy([{**SYSTEM, "appPassword": {"allowedIps": ["10.0.0.0/8"], "permissions": ["authenticate", "emailSend"]}}], smtp_check=False)
check("job succeeds", run_job(narrowed) == 0)
check("same credential, now refused from here", secret("mailer-smtp")[1]["credential"] == reference and not smtp_login(ADDRESS, password))
config = deploy([SYSTEM])
check("job with the original restrictions succeeds", run_job(config) == 0)
check("same credential, accepted again", secret("mailer-smtp")[1]["credential"] == reference and smtp_login(ADDRESS, password))
check("unchanged restrictions: no update on the next run", run_job(config) == 0 and "restrictions" not in LAST_OUTPUT[0])

print("-- credential revoked in Stalwart")
destroy("system", first)
check("job succeeds", run_job(config) == 0)
data3, ann3 = secret("mailer-smtp")
second = ann3.get("credential")
check("exactly one new credential", second and second != first and managed("system") == {second}, (second, managed("system")))
check("new password published and working", data3["password"] != data["password"] and smtp_login(ADDRESS, data3["password"]))

print("-- interrupted run: a credential created but never recorded")
jmap("x:AppPassword/set", {"accountId": account_id("system"), "create": {"o": {"description": "managed:stalwart/system"}}})
jmap("x:AppPassword/set", {"accountId": account_id("system"), "create": {"o": {"description": "managed:stalwart/system #deadbeef"}}})
jmap("x:AppPassword/set", {"accountId": account_id("system"), "create": {"f": {"description": "made by a user"}}})
check("job succeeds", run_job(config) == 0)
remaining = [p.get("description") for p in app_passwords("system").values()]
check("unrecorded managed credentials revoked", sorted(remaining) == sorted(["made by a user", f"managed:stalwart/system #{second}"]), remaining)
check("recorded credential untouched", secret("mailer-smtp")[1].get("credential") == second)

print("-- a key added to secret.keys")
more = {**SYSTEM, "secret": {**SYSTEM["secret"], "keys": SYSTEM["secret"]["keys"] + ["SMTP_PASSWORD"]}}
config_more = deploy([more])
check("job succeeds", run_job(config_more) == 0)
data4, ann4 = secret("mailer-smtp")
check("new key filled with the same password, no new credential", data4.get("SMTP_PASSWORD") == data3["password"] and ann4.get("credential") == second)

print("-- the Secret lost its password")
kubectl("-n", "mail", "patch", "secret", "mailer-smtp", "--type", "json", "-p",
        json.dumps([{"op": "remove", "path": f"/data/{k}"} for k in ("password", "OPENPROJECT_SMTP__PASSWORD", "SMTP_PASSWORD")]))
check("job succeeds", run_job(config_more) == 0)
data5, ann5 = secret("mailer-smtp")
check("replaced: new credential, the lost one revoked", ann5.get("credential") not in (second, None) and managed("system") == {ann5.get("credential")}, (ann5, managed("system")))
check("new password works", smtp_login(ADDRESS, data5.get("password", "")))

print("-- concurrent runs")
annotate("mailer-smtp", f"lock=other-job@{int(time.time())}")
check("a fresh lock of another Job blocks the run", run_job(config_more, job_uid="job-2") != 0)
annotate("mailer-smtp", f"lock=job-2@{int(time.time())}")
check("the same Job takes over its own lock", run_job(config_more, job_uid="job-2") == 0 and "lock" not in secret("mailer-smtp")[1])
annotate("mailer-smtp", f"lock=other-job@{int(time.time()) - 3600}")
check("an expired lock is taken over", run_job(config_more, job_uid="job-3") == 0)

print("-- failing SMTP check: no credential is kept")
destroy("system", secret("mailer-smtp")[1]["credential"])
blocked = {**more, "appPassword": {"allowedIps": ["10.0.0.0/8"], "permissions": ["authenticate", "emailSend"]}}
config_blocked = deploy([blocked])
before = secret("mailer-smtp")
check("job fails when the new password cannot log in from here", run_job(config_blocked) != 0)
check("no new credential kept in Stalwart", managed("system") == set(), managed("system"))
check("Secret unchanged", secret("mailer-smtp") == before)

print("-- Stalwart unreachable: no change")
config_down = {**config_more, "stalwartUrl": "http://127.0.0.1:1"}
before = secret("mailer-smtp")
check("job fails", run_job(config_down) != 0)
check("Secret unchanged and unlocked", secret("mailer-smtp") == before)

print("-- real database reset: Stalwart on an empty database")
generation_before, _ = statefulset()
reference = secret("mailer-smtp")[1]["credential"]
reset_stalwart()
config = deploy([more])
check("marker gone with the database", marker() is None)
check("job succeeds", run_job(config) == 0)
check("restarted although the hash on the StatefulSet is unchanged", statefulset()[0] == generation_before + 1, statefulset())
check("marker recorded again", marker() == config["restart"]["settingsHash"])
data7, ann7 = secret("mailer-smtp")
check("account recreated with a new credential", ann7.get("credential") not in (reference, None) and smtp_login(ADDRESS, data7.get("password", "")))
check("job again: nothing to do", run_job(config) == 0 and statefulset()[0] == generation_before + 1 and secret("mailer-smtp")[1]["credential"] == ann7["credential"])

print("-- existing Secret (secret.create: false), in another namespace")
kubectl("-n", "apps", "create", "secret", "generic", "precreated", "--from-literal=unrelated=keep-me")
bot = {"id": "bot", "name": "bot", "appPassword": {"allowedIps": ["127.0.0.0/8", "::1/128"]},
       "secret": {"name": "precreated", "namespace": "apps", "create": False}}
docs = render({**BASE, "systemAccounts": {"accounts": [bot]}})
check("no Secret rendered for create: false", not any(d["kind"] == "Secret" and d["metadata"]["name"] == "precreated" for d in docs))
config_bot = deploy([bot], smtp_check=False)
check("job succeeds", run_job(config_bot) == 0)
data6, _ = secret("precreated", namespace="apps")
check("password added, other keys kept", data6.get("unrelated") == "keep-me" and smtp_login("bot@example.org", data6.get("password", "")), data6.keys())

print()
if failures:
    sys.exit(f"{len(failures)} check(s) failed: {failures}")
print("all reconcile checks passed")
