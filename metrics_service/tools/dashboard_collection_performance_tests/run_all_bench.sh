#!/bin/bash
#
# Run the full dashboard-collection performance test matrix:
#
#   For each scale in --scales (default: 1 2 3 4):
#     1. Truncate the AWX activitystream junction tables
#     2. Fill the AWX DB for the scale (fill_data.py)
#     3. Run the internal collection benchmark (benchmark_dashboard_collection.py)
#     4. Run the HTTP API benchmark (benchmark_dashboard_api.py, DIRECT_DB=true)
#     5. Run the API SLO benchmark (benchmark_dashboard_api_slo.py, DIRECT_DB=true)
#
# Assumes the one-time setup from BENCHMARK_JENKINS_PODMAN.md /
# BENCHMARK_JENKINS_PODMAN_API.md / BENCHMARK_JENKINS_PODMAN_API_SLO.md has
# already been done (scripts copied into the container's /tmp, mock_awx
# installed, psutil installed).
#
# Usage:
#   ./run_all_bench.sh --awx-db-password '<password>' [options]
#
# Required:
#   --awx-db-password PASS   Password for the AWX database user
#
# Options:
#   --user NAME              Metrics-service admin username for the API benchmark (default: admin)
#   --web-container NAME     metrics-service web container name (default: automation-metrics-web)
#   --pg-container NAME      AWX postgres container name (default: postgresql)
#   --awx-db-host HOST       AWX DB host, as reachable from the web container (default: aio-0)
#   --awx-db-port PORT       AWX DB port (default: 5432)
#   --awx-db-name NAME       AWX DB name (default: awx)
#   --awx-db-user USER       AWX DB user (default: awx)
#   --test-since DATE        Period start, YYYY-MM-DD (default: 2026-04-17)
#   --test-until DATE        Period end, YYYY-MM-DD (default: 2026-07-16)
#   --scales "1 2 3 4"       Space-separated list of scales to run (default: "1 2 3 4")
#   --results-dir DIR        Where result .txt files are written, on the container's
#                            filesystem (default: /home/ansible)
#   --skip-collection        Skip the internal collection benchmark
#   --skip-api               Skip the HTTP API benchmark
#   --skip-slo               Skip the API SLO benchmark
#   -h, --help               Show this help and exit
#
# Example:
#   ./run_all_bench.sh \
#     --awx-db-password 'AdminPG!Password!Metrics' \
#     --user admin \
#     --scales "1 2"

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
BENCHMARK_USER="admin"
WEB_CONTAINER="automation-metrics-web"
PG_CONTAINER="postgresql"
AWX_DB_HOST="aio-0"
AWX_DB_PORT="5432"
AWX_DB_NAME="awx"
AWX_DB_USER="awx"
AWX_DB_PASSWORD=""
# Note: kept as a rolling ~90-day window near "now" so fill_data.py generates
# realistic-looking synthetic data. Always passed explicitly to the benchmark
# scripts below, so it does NOT need to match their standalone 2024 fallback
# defaults (those only apply when TEST_SINCE/TEST_UNTIL are unset).
TEST_SINCE="2026-04-17"
TEST_UNTIL="2026-07-16"
SCALES="1 2 3 4"
RESULTS_DIR="/home/ansible"
SKIP_COLLECTION=false
SKIP_API=false
SKIP_SLO=false

usage() {
    sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --user) BENCHMARK_USER="$2"; shift 2 ;;
        --web-container) WEB_CONTAINER="$2"; shift 2 ;;
        --pg-container) PG_CONTAINER="$2"; shift 2 ;;
        --awx-db-host) AWX_DB_HOST="$2"; shift 2 ;;
        --awx-db-port) AWX_DB_PORT="$2"; shift 2 ;;
        --awx-db-name) AWX_DB_NAME="$2"; shift 2 ;;
        --awx-db-user) AWX_DB_USER="$2"; shift 2 ;;
        --awx-db-password) AWX_DB_PASSWORD="$2"; shift 2 ;;
        --test-since) TEST_SINCE="$2"; shift 2 ;;
        --test-until) TEST_UNTIL="$2"; shift 2 ;;
        --scales) SCALES="$2"; shift 2 ;;
        --results-dir) RESULTS_DIR="$2"; shift 2 ;;
        --skip-collection) SKIP_COLLECTION=true; shift ;;
        --skip-api) SKIP_API=true; shift ;;
        --skip-slo) SKIP_SLO=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
    esac
done

if [[ -z "$AWX_DB_PASSWORD" ]]; then
    echo "ERROR: --awx-db-password is required" >&2
    exit 1
fi

echo "================================================================================"
echo "  Dashboard Collection Performance Tests"
echo "  Scales:        $SCALES"
echo "  Period:        $TEST_SINCE -> $TEST_UNTIL"
echo "  Web container: $WEB_CONTAINER"
echo "  PG container:  $PG_CONTAINER"
echo "  AWX DB:        $AWX_DB_USER@$AWX_DB_HOST:$AWX_DB_PORT/$AWX_DB_NAME"
echo "  API user:      $BENCHMARK_USER"
echo "  Results dir:   $RESULTS_DIR"
echo "================================================================================"

# ---------------------------------------------------------------------------
# Pin checksums for the files we exec()/run inside the container's /tmp so a
# later scale iteration aborts instead of silently running tampered code if
# something else with exec access into the container overwrote /tmp between
# runs.
# ---------------------------------------------------------------------------
FILL_SCRIPT="/tmp/fill_data.py"
COLLECTION_BENCH_SCRIPT="/tmp/benchmark_dashboard_collection.py"
API_BENCH_SCRIPT="/tmp/benchmark_dashboard_api.py"
SLO_BENCH_SCRIPT="/tmp/benchmark_dashboard_api_slo.py"

verify_checksum() {
    local path="$1" expected="$2" current
    current=$(podman exec "$WEB_CONTAINER" sha256sum "$path" | awk '{print $1}')
    if [[ "$current" != "$expected" ]]; then
        echo "ERROR: $path checksum changed since script start (expected $expected, got $current)." >&2
        echo "       Possible tampering in the container's /tmp — aborting." >&2
        exit 1
    fi
}

FILL_SHA=$(podman exec "$WEB_CONTAINER" sha256sum "$FILL_SCRIPT" | awk '{print $1}')
[[ "$SKIP_COLLECTION" == false ]] && COLLECTION_BENCH_SHA=$(podman exec "$WEB_CONTAINER" sha256sum "$COLLECTION_BENCH_SCRIPT" | awk '{print $1}')
[[ "$SKIP_API" == false ]] && API_BENCH_SHA=$(podman exec "$WEB_CONTAINER" sha256sum "$API_BENCH_SCRIPT" | awk '{print $1}')
[[ "$SKIP_SLO" == false ]] && SLO_BENCH_SHA=$(podman exec "$WEB_CONTAINER" sha256sum "$SLO_BENCH_SCRIPT" | awk '{print $1}')

for SCALE in $SCALES; do
    echo
    echo "=== Scale $SCALE ==="

    # -------------------------------------------------------------------
    # 1. Truncate activitystream junction tables (required before each fill)
    # -------------------------------------------------------------------
    echo "--- Step 1: Truncate activitystream tables ---"
    podman exec "$PG_CONTAINER" psql -U "$AWX_DB_USER" -d "$AWX_DB_NAME" -c "
    TRUNCATE TABLE
      main_activitystream_job_template,
      main_activitystream_organization,
      main_activitystream_credential,
      main_activitystream_execution_environment,
      main_activitystream_host;
    "

    # -------------------------------------------------------------------
    # 2. Fill the AWX DB for this scale
    # -------------------------------------------------------------------
    echo "--- Step 2: Fill AWX DB (scale $SCALE) ---"
    verify_checksum "$FILL_SCRIPT" "$FILL_SHA"
    podman exec \
        -e METRICS_UTILITY_PATH=/tmp/mu \
        -e METRICS_UTILITY_DB_HOST="$AWX_DB_HOST" \
        -e METRICS_UTILITY_DB_PORT="$AWX_DB_PORT" \
        -e METRICS_UTILITY_DB_NAME="$AWX_DB_NAME" \
        -e METRICS_UTILITY_DB_USER="$AWX_DB_USER" \
        -e METRICS_UTILITY_DB_PASSWORD="$AWX_DB_PASSWORD" \
        "$WEB_CONTAINER" \
        python3.12 /tmp/fill_data.py --scale "$SCALE" --period-start "$TEST_SINCE" --period-end "$TEST_UNTIL"

    # -------------------------------------------------------------------
    # 3. Internal collection benchmark
    # -------------------------------------------------------------------
    if [[ "$SKIP_COLLECTION" == false ]]; then
        echo "--- Step 3: Internal collection benchmark (scale $SCALE) ---"
        verify_checksum "$COLLECTION_BENCH_SCRIPT" "$COLLECTION_BENCH_SHA"
        podman exec \
            -e TEST_SINCE="$TEST_SINCE" \
            -e TEST_UNTIL="$TEST_UNTIL" \
            -e DB_NAME="$AWX_DB_NAME" \
            "$WEB_CONTAINER" \
            python3.12 manage.py shell -c "
exec(open('/tmp/benchmark_dashboard_collection.py').read())
run_dashboard_collection_benchmark()
" | tee "${RESULTS_DIR}/results_scale${SCALE}_internal.txt"
    else
        echo "--- Step 3: Internal collection benchmark (scale $SCALE) — skipped ---"
    fi

    # -------------------------------------------------------------------
    # 4. HTTP API benchmark (direct-DB mode; production HTTP API only
    #    accepts a gateway JWT, see BENCHMARK_JENKINS_PODMAN_API.md)
    # -------------------------------------------------------------------
    if [[ "$SKIP_API" == false ]]; then
        echo "--- Step 4: API benchmark (scale $SCALE) ---"
        verify_checksum "$API_BENCH_SCRIPT" "$API_BENCH_SHA"
        podman exec \
            -e METRICS_SERVICE_LOG_LEVEL=WARNING \
            -e METRICS_SERVICE_FEATURE__DASHBOARD_COLLECTION=true \
            -e DIRECT_DB=true \
            -e BENCHMARK_USER="$BENCHMARK_USER" \
            -e TEST_SINCE="$TEST_SINCE" \
            -e TEST_UNTIL="$TEST_UNTIL" \
            -e DB_NAME="$AWX_DB_NAME" \
            "$WEB_CONTAINER" \
            python3.12 manage.py shell -c "
exec(open('/tmp/benchmark_dashboard_api.py').read())
main()
" | tee "${RESULTS_DIR}/results_scale${SCALE}_api.txt"
    else
        echo "--- Step 4: API benchmark (scale $SCALE) — skipped ---"
    fi

    # -------------------------------------------------------------------
    # 5. API SLO benchmark (direct-DB mode; reads whatever JobData/
    #    TemplateMetadata is currently in place, see
    #    BENCHMARK_JENKINS_PODMAN_API_SLO.md)
    # -------------------------------------------------------------------
    if [[ "$SKIP_SLO" == false ]]; then
        echo "--- Step 5: API SLO benchmark (scale $SCALE) ---"
        verify_checksum "$SLO_BENCH_SCRIPT" "$SLO_BENCH_SHA"
        podman exec \
            -e METRICS_SERVICE_LOG_LEVEL=WARNING \
            -e METRICS_SERVICE_FEATURE__DASHBOARD_COLLECTION=true \
            -e DIRECT_DB=true \
            -e BENCHMARK_USER="$BENCHMARK_USER" \
            -e TEST_SINCE="$TEST_SINCE" \
            -e TEST_UNTIL="$TEST_UNTIL" \
            -e DB_NAME="$AWX_DB_NAME" \
            "$WEB_CONTAINER" \
            python3.12 manage.py shell -c "
exec(open('/tmp/benchmark_dashboard_api_slo.py').read())
main()
" | tee "${RESULTS_DIR}/results_scale${SCALE}_api_slo.txt"
    else
        echo "--- Step 5: API SLO benchmark (scale $SCALE) — skipped ---"
    fi

done

echo
echo "================================================================================"
echo "  Done. Results written to ${RESULTS_DIR}/results_scale*_{internal,api,api_slo}.txt"
echo "================================================================================"

