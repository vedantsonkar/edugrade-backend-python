#!/usr/bin/env bash
# Deploy only the current master commit after its GitHub Actions CI succeeds.
set -Eeuo pipefail
exec 9>/run/edugrade-deploy.lock
flock -n 9 || exit 0

repo='vedantsonkar/edugrade-backend-python'
api="https://api.github.com/repos/${repo}"
root=/opt/edugrade
request() { curl -fsSL --max-time 20 -H 'Accept: application/vnd.github+json' -H 'User-Agent: edugrade-uat-deployer' "$1"; }
current_head() { request "$api/commits/master" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])'; }

sha=$(current_head)
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || exit 1
[[ -f "$root/deployed-sha" && "$(cat "$root/deployed-sha")" == "$sha" ]] && exit 0
passed=$(request "$api/actions/workflows/backend-ci.yml/runs?branch=master&event=push&status=completed&per_page=1" | python3 -c 'import json,sys; r=json.load(sys.stdin).get("workflow_runs",[]); print(r[0]["head_sha"] if r and r[0]["conclusion"] == "success" else "")')
[[ "$passed" == "$sha" ]] || { echo 'Waiting for CI to pass for the current master commit'; exit 0; }

work=$(mktemp -d "$root/build.XXXXXXXX")
cleanup() { docker rm -f edugrade-api-candidate >/dev/null 2>&1 || true; rm -rf -- "$work"; }
trap cleanup EXIT
curl -fsSL --max-time 120 "https://codeload.github.com/$repo/tar.gz/$sha" -o "$work/source.tar.gz"
mkdir "$work/source"
tar -xzf "$work/source.tar.gz" -C "$work/source" --strip-components=1
image="edugrade-uat:$sha"
docker build -q -t "$image" -f "$work/source/docker/Dockerfile" "$work/source"
docker run -d --name edugrade-api-candidate --network edugrade-uat --env-file "$root/backend.env" "$image" >/dev/null
ready() { docker exec "$1" python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8080/api/v1/ready",timeout=3).read()' >/dev/null 2>&1; }
healthy=false
for _ in $(seq 1 60); do if ready edugrade-api-candidate; then healthy=true; break; fi; sleep 2; done
[[ "$healthy" == true ]] || { echo 'Candidate failed readiness; existing API retained'; exit 1; }
[[ "$(current_head)" == "$sha" ]] || { echo 'Master changed during build; retrying next poll'; exit 0; }
docker rm -f edugrade-api-candidate >/dev/null

# Keep the previous container for rollback. No database or volume is removed.
docker rm -f edugrade-api-previous >/dev/null 2>&1 || true
docker stop edugrade-api >/dev/null
docker rename edugrade-api edugrade-api-previous
rollback() {
  docker rm -f edugrade-api >/dev/null 2>&1 || true
  docker rename edugrade-api-previous edugrade-api
  docker start edugrade-api >/dev/null
  docker exec edugrade-caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 || true
}
if ! docker run -d --name edugrade-api --network edugrade-uat --restart unless-stopped --env-file "$root/backend.env" -p 127.0.0.1:18080:8080 "$image" >/dev/null; then rollback; exit 1; fi
healthy=false
for _ in $(seq 1 60); do if ready edugrade-api; then healthy=true; break; fi; sleep 2; done
if [[ "$healthy" != true ]]; then echo 'Replacement failed readiness; rolling back'; rollback; exit 1; fi
if ! docker exec edugrade-caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then rollback; exit 1; fi
printf '%s\n' "$sha" > "$root/deployed-sha"
echo "Deployed commit $sha"
