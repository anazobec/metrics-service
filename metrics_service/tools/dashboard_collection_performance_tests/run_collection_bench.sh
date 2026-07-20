#!/bin/bash
set -euo pipefail

# Usage:
#   AWX_DB_PASSWORD=password ./run_collection_bench.sh

CONTAINER="automation-metrics-web"
FILL_SCRIPT="/tmp/fill_data.py"
BENCH_SCRIPT="/tmp/benchmark_dashboard_collection.py"

# Pin a checksum for each file we exec()/run inside the container so a later
# scale iteration aborts instead of silently running tampered code if
# something else with exec access into the container overwrote /tmp between
# runs (see BENCHMARK_JENKINS_PODMAN.md — Security notes).
FILL_SHA=$(podman exec "$CONTAINER" sha256sum "$FILL_SCRIPT" | awk '{print $1}')
BENCH_SHA=$(podman exec "$CONTAINER" sha256sum "$BENCH_SCRIPT" | awk '{print $1}')

verify_checksum() {
  local path="$1" expected="$2" current
  current=$(podman exec "$CONTAINER" sha256sum "$path" | awk '{print $1}')
  if [[ "$current" != "$expected" ]]; then
    echo "ERROR: $path checksum changed since script start (expected $expected, got $current)." >&2
    echo "       Possible tampering in the container's /tmp — aborting." >&2
    exit 1
  fi
}

for SCALE in 1 2 3 4; do
  echo "=== Scale $SCALE ==="

  # Truncate activitystream junction tables
  podman exec postgresql psql -U awx -d awx -c "
  TRUNCATE TABLE
    main_activitystream_job_template,
    main_activitystream_organization,
    main_activitystream_credential,
    main_activitystream_execution_environment,
    main_activitystream_host;
  "

  verify_checksum "$FILL_SCRIPT" "$FILL_SHA"

  # Fill
  podman exec \
    -e METRICS_UTILITY_PATH=/tmp/mu \
    -e METRICS_UTILITY_DB_HOST=aio-0 \
    -e METRICS_UTILITY_DB_PORT=5432 \
    -e METRICS_UTILITY_DB_NAME=awx \
    -e METRICS_UTILITY_DB_USER=awx \
    -e METRICS_UTILITY_DB_PASSWORD="$AWX_DB_PASSWORD" \
    "$CONTAINER" \
    python3.12 "$FILL_SCRIPT" --scale "$SCALE"

  verify_checksum "$BENCH_SCRIPT" "$BENCH_SHA"

  # Benchmark
  podman exec "$CONTAINER" \
    python3.12 manage.py shell -c "
exec(open('$BENCH_SCRIPT').read())
run_dashboard_collection_benchmark()
" \
    | tee /home/ansible/results_scale${SCALE}_internal.txt

done
