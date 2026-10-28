#!/usr/bin/env bash
# End-to-end test of the chart's provisioning and reconcile Jobs in a real cluster: the chart
# installed with Helm on a throwaway kind cluster, the Jobs running as hooks in pods, and a
# consumer in another namespace that reads the system account's Secret and is only ready once
# its SMTP login works.
#   1. one helm install with settings (an SMTP listener on 2525, an OIDC directory whose issuer
#      does not exist) and a system account: the reconcile Job restarts Stalwart, so the
#      listener is bound, and publishes the app password, without the identity provider
#   2. the consumer, deployed after Stalwart, starts with the password and logs in
#   3. an unchanged helm upgrade restarts nothing and keeps the credential
#   4. changed settings restart Stalwart once
#   5. the credential is revoked in Stalwart (as after a database reset): the next upgrade
#      publishes a new one; the consumer logs in with it after its restart
#   6. helm uninstall removes the Secret with the release
#
# Requires kind, kubectl, helm, python3 with PyYAML and Docker (or Podman with
# KIND_EXPERIMENTAL_PROVIDER=podman). KEEP=1 leaves the cluster running.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
chart="$(cd "$here/../.." && pwd)"
work="$(mktemp -d)"
cluster="stalwart-e2e-$(basename "$work" | tr -cd 'a-z0-9' | tail -c 8)"
export KUBECONFIG="$work/kubeconfig"
created=0
forward_pid=""
failures=0

cleanup() {
  [ -n "$forward_pid" ] && kill "$forward_pid" 2>/dev/null || true
  if [ "$created" = 1 ] && [ "${KEEP:-0}" = 1 ]; then
    echo "Cluster $cluster kept: KUBECONFIG=$KUBECONFIG kind delete cluster --name $cluster"
    return
  fi
  if [ "$created" = 1 ] && ! output=$(kind delete cluster --name "$cluster" 2>&1); then
    echo "WARN could not delete kind cluster $cluster:" >&2
    echo "$output" >&2
  fi
  rm -rf "$work"
}
trap cleanup EXIT

step() { printf '\n== %s\n' "$*"; }
ok() { echo "ok   $*"; }
fail() { echo "FAIL $*"; failures=$((failures + 1)); }
check() { # check NAME COMMAND...: runs the command, reports ok or FAIL
  local name=$1; shift
  if "$@"; then ok "$name"; else fail "$name"; fi
}

diagnose() {
  echo "-- diagnostics" >&2
  kubectl get pods,jobs -A -o wide 2>&1 | grep -v kube-system >&2 || true
  for job in $(kubectl -n mail get jobs -o name 2>/dev/null); do
    echo "-- logs of $job" >&2
    kubectl -n mail logs "$job" --all-containers --tail=50 >&2 2>&1 || true
  done
  kubectl -n mail logs statefulset/stalwart --tail=30 >&2 2>&1 || true
  kubectl -n apps logs deploy/consumer --tail=20 >&2 2>&1 || true
}
die() { echo "FAIL $*" >&2; diagnose; exit 1; }

image() { # image KEY...: the pinned image of a values.yaml entry
  python3 - "$chart/values.yaml" "$@" <<'PY'
import sys, yaml
image = yaml.safe_load(open(sys.argv[1]))
for key in sys.argv[2:]:
    image = image[key]
print("{registry}/{repository}:{tag}@{digest}".format(**image))
PY
}

annotation() { # annotation KEY: an annotation of the system account's Secret
  kubectl -n apps get secret mailer-smtp -o jsonpath="{.metadata.annotations.stalwart\.s7n\.systems/$1}" 2>/dev/null
}

generation() { kubectl -n mail get statefulset stalwart -o jsonpath='{.metadata.generation}'; }

helm_upgrade() { # helm_upgrade [args...]: the release with the test values
  helm upgrade --install stalwart "$chart" --namespace mail -f "$here/kind-values.yaml" -f "$here/kind-accounts.yaml" \
    --wait --timeout 15m "$@" >"$work/helm.log" 2>&1 || { cat "$work/helm.log" >&2; return 1; }
}

consumer_pod() { # the consumer's running pod that is not being deleted
  kubectl -n apps get pods -l app=consumer -o json | python3 -c '
import json, sys
pods = [p["metadata"]["name"] for p in json.load(sys.stdin)["items"]
        if p["status"].get("phase") == "Running" and not p["metadata"].get("deletionTimestamp")]
print(pods[0] if len(pods) == 1 else "")'
}
logged_in() { kubectl -n apps logs "pod/$1" | grep -q 'LOGIN OK'; }
wait_logged_in() { # wait_logged_in POD: up to 60 seconds
  for _ in $(seq 1 30); do logged_in "$1" && return 0; sleep 2; done
  return 1
}

step "prerequisites"
for tool in kind kubectl helm python3; do
  command -v "$tool" >/dev/null || { echo "FAIL $tool is not installed"; exit 1; }
done
python3 -c 'import yaml' 2>/dev/null || { echo "FAIL python3 needs PyYAML"; exit 1; }
ok "tools present"

step "kind cluster"
kind create cluster --name "$cluster" --kubeconfig "$KUBECONFIG" --wait 180s >/dev/null 2>&1
created=1
ok "cluster $cluster"

step "1. helm install: settings and a system account, without the identity provider"
kubectl create namespace mail >/dev/null
kubectl create namespace apps >/dev/null
admin_password="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
kubectl -n mail create secret generic stalwart-admin --from-literal=password="$admin_password" \
  --from-literal=STALWART_RECOVERY_ADMIN="admin:$admin_password" >/dev/null
helm_upgrade || die "helm install"
ok "helm install succeeded: both hook Jobs finished"
check "hook Jobs deleted after success" test -z "$(kubectl -n mail get jobs -o name)"
check "Stalwart restarted once for its settings (generation 2)" test "$(generation)" = 2
check "settings hash on the pod template" test -n \
  "$(kubectl -n mail get statefulset stalwart -o jsonpath='{.spec.template.metadata.annotations.stalwart\.s7n\.systems/settings}')"
first="$(annotation credential)"
check "credential published ($first), checked over the listener bound by the restart" test -n "$first"
check "Secret holds a password" test -n "$(kubectl -n apps get secret mailer-smtp -o jsonpath='{.data.password}')"
sa="system:serviceaccount:mail:stalwart-reconcile"
check "RBAC: the Job may update its Secret" test "$(kubectl auth can-i update secrets/mailer-smtp -n apps --as "$sa")" = "yes"
check "RBAC: the Job may restart Stalwart" test "$(kubectl auth can-i patch statefulsets/stalwart -n mail --as "$sa")" = "yes"
check "RBAC: the Job may not read other Secrets" test "$(kubectl auth can-i get secrets/stalwart-admin -n mail --as "$sa")" = "no"
check "RBAC: the Job may not list Secrets" test "$(kubectl auth can-i list secrets -n apps --as "$sa")" = "no"

step "2. the consumer, deployed after Stalwart, starts with the password"
sed "s#image: \"IMAGE\"#image: \"$(image provisioning reconcile image)\"#" "$here/kind-consumer.yaml" | kubectl apply -f - >/dev/null
check "consumer Deployment available" kubectl -n apps wait deployment/consumer --for=condition=Available --timeout=180s
for _ in $(seq 1 30); do [ -n "$(consumer_pod)" ] && break; sleep 2; done
pod_first="$(consumer_pod)"
check "consumer logged in on its first start" wait_logged_in "$pod_first"

step "3. unchanged helm upgrade"
helm_upgrade || die "unchanged helm upgrade"
check "Stalwart not restarted" test "$(generation)" = 2
check "same credential" test "$(annotation credential)" = "$first"
check "consumer untouched" test "$(consumer_pod)" = "$pod_first"

step "4. changed settings"
helm_upgrade --set hostname=mx.example.org || die "helm upgrade with changed settings"
check "Stalwart restarted once more (generation 3)" test "$(generation)" = 3
check "same credential" test "$(annotation credential)" = "$first"

step "5. credential revoked in Stalwart, as after a database reset"
kubectl -n mail port-forward svc/stalwart-management 18080:8080 >/dev/null 2>&1 &
forward_pid=$!
sleep 3
python3 - "$admin_password" "$first" <<'PY' || die "revoking the credential"
import base64, json, sys, urllib.request
password, reference = sys.argv[1], sys.argv[2]
def jmap(method, args):
    body = json.dumps({"using": ["urn:ietf:params:jmap:core", "urn:stalwart:jmap"], "methodCalls": [[method, args, "c"]]}).encode()
    req = urllib.request.Request("http://127.0.0.1:18080/jmap/", data=body, method="POST")
    req.add_header("Authorization", "Basic " + base64.b64encode(f"admin:{password}".encode()).decode())
    req.add_header("Content-Type", "application/json")
    return json.load(urllib.request.urlopen(req, timeout=30))["methodResponses"][0][1]
account = jmap("x:Account/query", {"filter": {"name": "mailer"}})["ids"][0]
ids = [p["id"] for p in jmap("x:AppPassword/get", {"accountId": account})["list"] if (p.get("description") or "").endswith("#" + reference)]
assert ids, "recorded credential not found in Stalwart"
jmap("x:AppPassword/set", {"accountId": account, "destroy": ids})
print(f"     revoked app password {ids[0]} ({reference})")
PY
kill "$forward_pid" 2>/dev/null || true; forward_pid=""
helm_upgrade --set hostname=mx.example.org || die "helm upgrade after the revocation"
second="$(annotation credential)"
check "a new credential was published ($second)" test -n "$second" -a "$second" != "$first"
check "the running consumer is left alone" test "$(consumer_pod)" = "$pod_first"
kubectl -n apps rollout restart deployment/consumer >/dev/null
kubectl -n apps rollout status deployment/consumer --timeout=180s >/dev/null || die "consumer restart"
kubectl -n apps wait --for=delete "pod/$pod_first" --timeout=120s >/dev/null 2>&1 || true
pod_second="$(consumer_pod)"
check "consumer restarted ($pod_first -> $pod_second)" test -n "$pod_second" -a "$pod_second" != "$pod_first"
check "the restarted consumer logged in with the new password" wait_logged_in "$pod_second"

step "6. helm uninstall"
helm uninstall stalwart -n mail --wait >/dev/null
check "the Secret is removed with the release" sh -c "! kubectl -n apps get secret mailer-smtp >/dev/null 2>&1"
check "the Job's Roles are gone" sh -c "! kubectl -n apps get role stalwart-reconcile >/dev/null 2>&1 && ! kubectl -n mail get role stalwart-reconcile >/dev/null 2>&1"

echo
if [ "$failures" -gt 0 ]; then
  diagnose
  echo "$failures check(s) failed"
  exit 1
fi
echo "== all reconcile checks in kind passed"
