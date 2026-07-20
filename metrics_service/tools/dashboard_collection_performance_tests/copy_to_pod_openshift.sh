#!/usr/bin/env bash
# Copy the dashboard collection perf test scripts (fill_data.py, mock_awx,
# metrics-utility's anonymized_db_perf_data helpers) into the running
# metrics-service pod on an OpenShift instance.
#
# The `metrics-web` container image has no `tar`/`rsync`, so `oc cp` fails
# with "exec: tar: executable file not found in $PATH". This script copies
# files instead via `base64` encode/decode (both `base64` and `cat` are
# present in the image).
#
# Usage:
#   MU=/path/to/metrics-utility MS=/path/to/metrics-service ./copy_to_pod_openshift.sh
#   ./copy_to_pod_openshift.sh /path/to/metrics-utility /path/to/metrics-service
#
# Requires: oc CLI logged in (KUBECONFIG set / ~/.kube/config populated).

set -euo pipefail

NAMESPACE="${NAMESPACE:-aap}"
MU="${1:-${MU:-}}"
MS="${2:-${MS:-}}"

if [[ -z "$MU" || -z "$MS" ]]; then
  echo "ERROR: metrics-utility and metrics-service repo paths are required." >&2
  echo "Usage: MU=<path> MS=<path> $0" >&2
  echo "   or: $0 <metrics-utility-path> <metrics-service-path>" >&2
  exit 1
fi

if [[ ! -d "$MU/mock_awx" ]]; then
  echo "ERROR: '$MU' doesn't look like a metrics-utility checkout (missing mock_awx/)." >&2
  exit 1
fi

if [[ ! -f "$MS/metrics_service/tools/dashboard_collection_performance_tests/fill_data.py" ]]; then
  echo "ERROR: '$MS' doesn't look like a metrics-service checkout (missing fill_data.py)." >&2
  exit 1
fi

POD=$(oc get pods -n "$NAMESPACE" -o name | grep automationmetricsservice-web | head -1)
POD=${POD#pod/}
if [[ -z "$POD" ]]; then
  echo "ERROR: no automationmetricsservice-web pod found in namespace '$NAMESPACE'." >&2
  exit 1
fi
echo "Using pod: $POD (namespace: $NAMESPACE)"

copy_file() {
  local_path="$1"
  remote_path="$2"
  oc exec -n "$NAMESPACE" "$POD" -c metrics-web -- mkdir -p "$(dirname "$remote_path")"
  base64 -w0 "$local_path" | oc exec -i -n "$NAMESPACE" "$POD" -c metrics-web -- sh -c "base64 -d > '$remote_path'"
}

echo "Copying mock_awx ..."
find "$MU/mock_awx" -type f | while read -r f; do
  copy_file "$f" "/tmp/${f#"$MU"/}"
done

echo "Copying metrics-utility perf data helper scripts ..."
for f in fill_perf_db_data.py clean_all_data.py helpers.py modules.py; do
  copy_file "$MU/tools/anonymized_db_perf_data/$f" "/tmp/mu/tools/anonymized_db_perf_data/$f"
done

echo "Copying fill_data.py ..."
copy_file "$MS/metrics_service/tools/dashboard_collection_performance_tests/fill_data.py" "/tmp/fill_data.py"

echo "Copying prometheus_utils.py ..."
copy_file "$MS/metrics_service/tools/dashboard_collection_performance_tests/prometheus_utils.py" "/tmp/prometheus_utils.py"

echo "Copying benchmark_dashboard_collection.py ..."
copy_file "$MS/metrics_service/tools/dashboard_collection_performance_tests/benchmark_dashboard_collection.py" "/tmp/benchmark_dashboard_collection.py"

echo "Copying benchmark_dashboard_api.py ..."
copy_file "$MS/metrics_service/tools/dashboard_collection_performance_tests/benchmark_dashboard_api.py" "/tmp/benchmark_dashboard_api.py"

echo "Copying benchmark_dashboard_api_slo.py ..."
copy_file "$MS/metrics_service/tools/dashboard_collection_performance_tests/benchmark_dashboard_api_slo.py" "/tmp/benchmark_dashboard_api_slo.py"

echo "Done. Files are in /tmp on pod $POD:"
oc exec -n "$NAMESPACE" "$POD" -c metrics-web -- sh -c \
  'find /tmp/mock_awx -type f | wc -l; ls /tmp/mu/tools/anonymized_db_perf_data; ls -la /tmp/fill_data.py'
