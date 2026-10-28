#!/usr/bin/env bash
# End-to-end test of the reconcile Job (files/reconcile.py) against a real Stalwart and a real
# Kubernetes API server, with the RBAC the chart renders and a ServiceAccount token:
#
#   tests/e2e/run.sh
#
# Starts two containers on the host network (ports 8080, 2525 and 6443 must be free):
# Stalwart, and a k3s server without an agent (API, scheduler and controllers, no pods), so
# workload rollouts of zero replicas complete. The script itself runs on the host with
# python3, the way the Job runs it. Needs docker (or podman), kubectl, helm and python3 with
# PyYAML. KEEP=1 leaves the containers running.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
chart=$(cd "$here/../.." && pwd)

start_stalwart() { # start_stalwart DIR IMAGE CLI_IMAGE: Stalwart on an empty database, SMTP AUTH on 2525
  docker rm -f sa-test-stalwart >/dev/null 2>&1 || true
  printf '{"@type":"RocksDb","path":"/var/lib/stalwart"}\n' > "$1/config.json"
  docker run -d --name sa-test-stalwart --network host -v "$1/config.json:/etc/stalwart/config.json:ro" \
    -e STALWART_RECOVERY_ADMIN=admin:test-admin "$2" --config /etc/stalwart/config.json >/dev/null
  for _ in $(seq 1 60); do [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/healthz/ready)" = 200 ] && break; sleep 1; done
  cat > "$1/listener.ndjson" <<'EOF'
{"@type":"upsert","matchOn":["name"],"object":"NetworkListener","value":{"smtp-internal":{"bind":{"[::]:2525":true},"name":"smtp-internal","protocol":"smtp"}}}
{"@type":"update","object":"MtaStageAuth","value":{"saslMechanisms":{"else":"false","match":{"0":{"if":"local_port == 2525","then":"[plain, login]"}}},"require":{"else":"local_port != 25","match":{}}}}
EOF
  docker run --rm --network host -v "$1:/plan:ro" -e STALWART_URL=http://127.0.0.1:8080 -e STALWART_USER=admin \
    -e STALWART_PASSWORD=test-admin --entrypoint /usr/local/bin/stalwart-cli "$3" apply --file /plan/listener.ndjson --quiet
  # Stalwart binds new listeners at startup.
  docker restart sa-test-stalwart >/dev/null
  for _ in $(seq 1 60); do (exec 3<>/dev/tcp/127.0.0.1/2525) 2>/dev/null && break; sleep 1; done
}

# Called by check.py for a real database reset: Stalwart again, on an empty database.
if [ "${1:-}" = "--reset-stalwart" ]; then
  start_stalwart "$WORK" "$STALWART_IMAGE" "$CLI_IMAGE"
  exit 0
fi

work=$(mktemp -d)
# Read by the CLI container, which runs as a non-root user.
chmod 755 "$work"
cleanup() {
  if [ "${KEEP:-0}" != 1 ]; then
    docker rm -f sa-test-stalwart sa-test-k3s >/dev/null 2>&1 || true
    rm -rf "$work"
  else
    echo "kept: containers sa-test-stalwart, sa-test-k3s; files in $work"
  fi
}
trap cleanup EXIT

image() { # image KEY...: the pinned image of a values.yaml entry
  python3 - "$chart/values.yaml" "$@" <<'PY'
import sys, yaml
image = yaml.safe_load(open(sys.argv[1]))
for key in sys.argv[2:]:
    image = image[key]
print("{registry}/{repository}:{tag}@{digest}".format(**image))
PY
}
stalwart_image=$(image image)
cli_image=$(image provisioning image)

echo "== the Kubernetes API"
docker rm -f sa-test-k3s >/dev/null 2>&1 || true
docker run -d --name sa-test-k3s --network host --tmpfs /run --tmpfs /var/run docker.io/rancher/k3s:v1.37.1-k3s1@sha256:ca7f37d993d82ef0dcdcfecb2e0e2618ea541dbaffc620c8cedebe01a82acd0d \
  server --disable-agent --disable traefik,metrics-server,coredns,local-storage,servicelb --tls-san 127.0.0.1 >/dev/null
for _ in $(seq 1 90); do docker exec sa-test-k3s kubectl get --raw /readyz >/dev/null 2>&1 && break; sleep 1; done
docker exec sa-test-k3s cat /etc/rancher/k3s/k3s.yaml > "$work/kubeconfig"
export KUBECONFIG="$work/kubeconfig"
kubectl create namespace mail >/dev/null
kubectl create namespace apps >/dev/null
for _ in $(seq 1 30); do kubectl -n mail get serviceaccount default >/dev/null 2>&1 && break; sleep 1; done

echo "== Stalwart with an SMTP listener for SMTP AUTH (2525, plain)"
start_stalwart "$work" "$stalwart_image" "$cli_image"

echo "== checks"
CHART="$chart" WORK="$work" CLI_IMAGE="$cli_image" STALWART_IMAGE="$stalwart_image" python3 "$here/check.py"
