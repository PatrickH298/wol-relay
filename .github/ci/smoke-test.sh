#!/usr/bin/env bash
# Start the built image the way compose.example.yaml runs it (non-root,
# read-only, no capabilities) and check the HTTP API end to end. Magic
# packets go to 127.0.0.1 inside the container, so nothing hits a network.
#
# Usage: smoke-test.sh IMAGE
set -euo pipefail

image="$1"
name="wol-relay-smoke"
config="$(cd "$(dirname "$0")" && pwd)/smoke-config.json"
base="http://127.0.0.1:18098"
failures=0

cleanup() {
  echo "--- container logs"
  docker logs "$name" 2>&1 || true
  docker rm --force "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run --detach --name "$name" \
  --publish 127.0.0.1:18098:8098 \
  --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --volume "$config:/etc/wol-relay/config.json:ro" \
  --env CI_TOKEN_ALL=ci-env-token \
  "$image" >/dev/null

for _ in $(seq 1 40); do
  if curl --silent --fail "$base/health" >/dev/null; then break; fi
  if [ "$(docker inspect --format '{{.State.Running}}' "$name")" != "true" ]; then
    echo "container exited during startup"; exit 1
  fi
  sleep 0.25
done

# expect NAME EXPECTED_STATUS CURL_ARGS...
expect() {
  local label="$1" want="$2"; shift 2
  local got
  # curl still prints 000 on connection failure; don't let set -e abort.
  got=$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 15 "$@" || true)
  if [ "$got" = "$want" ]; then
    echo "ok   $label ($got)"
  else
    echo "FAIL $label: expected $want, got $got"
    failures=$((failures + 1))
  fi
}

expect "health"                      200 "$base/health"
expect "wake, env token, * access"   200 -X POST -H "Authorization: Bearer ci-env-token" "$base/wake/ci-other"
expect "wake, inline token, allowed" 200 -X POST -H "Authorization: Bearer ci-limited-inline-token" "$base/wake/ci-target"
expect "wake, not in allowed list"   404 -X POST -H "Authorization: Bearer ci-limited-inline-token" "$base/wake/ci-other"
expect "wake, unknown target"        404 -X POST -H "Authorization: Bearer ci-env-token" "$base/wake/nope"
expect "wake, wrong token"           401 -X POST -H "Authorization: Bearer wrong" "$base/wake/ci-target"
expect "wake, no token"              401 -X POST "$base/wake/ci-target"

if docker exec "$name" python3 -I /app/wol_relay.py --healthcheck >/dev/null; then
  echo "ok   in-container --healthcheck"
else
  echo "FAIL in-container --healthcheck"
  failures=$((failures + 1))
fi

user=$(docker exec "$name" id -u)
if [ "$user" != "0" ]; then
  echo "ok   runs as non-root (uid $user)"
else
  echo "FAIL container is running as root"
  failures=$((failures + 1))
fi

if [ "$failures" -ne 0 ]; then
  echo "$failures check(s) failed"
  exit 1
fi
echo "all checks passed"
