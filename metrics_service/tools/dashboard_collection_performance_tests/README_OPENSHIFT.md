# Running dashboard performance tests on an OpenShift instance

## Requirements

1. Install `jq` (used to decode DB credentials from secrets)
```bash
# Fedora/RHEL
sudo dnf install jq

# Debian/Ubuntu
sudo apt install jq
```

2. Install OpenShift CLI
```bash
# Download binary
curl --output oc.tar.gz "https://mirror.openshift.com/pub/openshift-v4/clients/oc/latest/linux/oc.tar.gz"

# Extract
mkdir -p ~/openshift

tar xvf oc.tar.gz -C ~/openshift

# Add extracted binaries to your shell config (.bashrc)
echo 'export PATH=$PATH:"$HOME/openshift"' >> ~/.bashrc

# Refresh your terminal session
source ~/.bashrc
# Note: this will refresh only your current session. You must restart your 
#       terminal for all new sessions in that terminal to take affect

# Verify installation
oc version
```

3. You must have a running [OpenShift instance on Jenkins](https://jenkins-csb-aap-main.dno.corp.redhat.com/job/AAPQA/job/Openshift%20Claim%20on%20Demand/)
4. Click/choose one (_01_Claim_)
5. Download `hive_cluster_claim_admin_kubeconfig.yml` onto your machine
6. Connect to the instance using `oc` CLI tool
```bash
export KUBECONFIG=path/to/hive_cluster_claim_admin_kubeconfig.yml

# Verify login
$ oc whoami
system:admin

$ oc whoami --show-server
https://...ocp4.testing.ansible.com:6443  # something similar should be printed (without the "...")
```

### Parameters to build your OpenShift instance with
- **CLUSTER_CLAIM_NAME**: _choose_
- **CLUSTER_POOL**: `aap-test-v417-x86-64-konflux`
- **CLUSTER_LIFE_TIME**: _choose_
- **CLUSTER_WAIT_TIMEOUT**: `7200`
- **INSTALL_PRODUCT_BUILD_CATALOG_SOURCE**: `yes`
- **PRODUCT_BUILD**: `latest-2.7-next-tier1` - change 2.7 to whatever version you're trying to test on
- **CATALOG_SOURCE_NAMESPACE**: `openshift-marketplace`
- **CATALOG_SOURCE_NAME**: `aap`


## Running benchmarks

In short, how the benchmarks are run:
1. truncate database
2. fill data for current scales (there a 4 scales)
3. run collection tests
4. run API tests (api and api_slo)
5. repeat 1 to 4 for all scales

### Setup database

1. Find the namespace and pods
```bash
oc get projects           # find your namespace, e.g. "aap"
oc get pods -n aap        # find metrics-service web pod + postgres pod
```
Look for a pod named like `aap-automationmetricsservice-web-<hash>-<hash>` (container: `metrics-web`)
and the shared postgres pod `aap-postgres-15-0`.

2. Get AWX and metrics-service DB credentials from secrets
```bash
oc get secret aap-controller-postgres-configuration -n aap -o json | jq -r '.data | to_entries[] | "\(.key) = \(.value | @base64d)"'

oc get secret aap-metrics-postgres-configuration -n aap -o json | jq -r '.data | to_entries[] | "\(.key) = \(.value | @base64d)"'
```
Note the `host`, `port`, `database`, `username`, `password` for the AWX (controller) DB — this is
what `fill_data.py` writes to.

3. Copy the perf test scripts into the pod

The `metrics-web` container image is minimal and does **not** have `tar` or `rsync`, so
`oc cp` (which shells out to `tar` on both ends) will fail with:
```
exec: "tar": executable file not found in $PATH
```
Instead, copy files via `base64` encode/decode (both `base64` and `cat` are present) — use
[`copy_to_pod_openshift.sh`](./copy_to_pod_openshift.sh):
```bash
MU=<path-to-metrics-utility-git-repo> MS=<path-to-metrics-service-git-repo> \
  ./copy_to_pod_openshift.sh
```
It auto-detects the `automationmetricsservice-web` pod (override namespace with `NAMESPACE=...`),
copies `mock_awx`, the metrics-utility perf data helper scripts, and all the perf test `.py`
scripts (`fill_data.py`, `prometheus_utils.py`, `benchmark_dashboard_collection.py`, `benchmark_dashboard_api.py`,
`benchmark_dashboard_api_slo.py`) into `/tmp` on the pod.

> **Why `mock_awx` instead of the real `awx` package:** `metrics_utility.prepare()` first tries
> `importlib.util.find_spec('awx')`. If the pod happens to have real AWX/controller modules
> installed, it uses those directly (and ignores the `METRICS_UTILITY_DB_*` env vars below —
> it'll use whatever DB the real `awx` package is already configured for). If not found, it falls
> back to the `AWX_PATH` env var (default `/awx_devel`) and looks for `awx` there instead.
>
> **Why not just drop `mock_awx` at the hardcoded site-packages fallback path:** the container
> runs as a non-root, arbitrary UID (`runAsNonRoot`, `runAsUser: 1000700000`) and
> `/usr/lib/python3.12/site-packages` is read-only for that user — `mock_awx`'s hardcoded
> fallback location (`site-packages/mock_awx`, next to the `metrics_utility` package) can't be
> written to. `/tmp` **is** writable, so point `AWX_PATH=/tmp/mock_awx` at a copy placed there
> instead (see step 4) — this sidesteps the read-only filesystem entirely.

4. Truncate `main_activitystream_*` tables in the AWX DB

`clean_all_data.py` (run automatically by `fill_data.py` before filling) deletes existing AWX
data, but FK constraints from `main_activitystream_*` junction tables block deletes on
`main_job`, `main_unifiedjob`, etc. Truncate them first, from the postgres pod:
```bash
oc exec -n aap aap-postgres-15-0 -- psql -U automationcontroller -d automationcontroller -c "
TRUNCATE TABLE
  main_activitystream_job_template,
  main_activitystream_organization,
  main_activitystream_credential,
  main_activitystream_execution_environment,
  main_activitystream_host,
  main_activitystream_job,
  main_activitystream
CASCADE;
"
```

5. Run `fill_data.py`

Every `oc exec` opens a fresh shell — **no environment persists between calls**, so all env vars
must be exported in the same `sh -c '...'` block as the script invocation:
```bash
POD=$(oc get pods -n aap -o name | grep automationmetricsservice-web | head -1); POD=${POD#pod/}

oc exec -n aap "$POD" -c metrics-web -- sh -c '
export METRICS_UTILITY_PATH=/tmp/mu
export AWX_PATH=/tmp/mock_awx
export METRICS_UTILITY_DB_HOST=aap-postgres-15
export METRICS_UTILITY_DB_PORT=5432
export METRICS_UTILITY_DB_NAME=automationcontroller
export METRICS_UTILITY_DB_USER=automationcontroller
export METRICS_UTILITY_DB_PASSWORD=<awx-db-password>
python3.12 /tmp/fill_data.py --scale 1
'
```
Drop `--period-start`/`--period-end` to use the default 90-day period, or pass a short range
(e.g. `--period-start 2024-01-01 --period-end 2024-01-01`) first to sanity-check the pipeline
before running a full scale.

6. Prepare a results folder:
```bash
mkdir -p ./results_openshift
```

### Run benchmark_dashboard_collection.py

Calls `_collect_data()` directly (no HTTP/dispatcherd layer) — same as the internal benchmark
in the main [README.md](./README.md), but run inside the OpenShift pod via `oc exec`.

1. Install `psutil` in the pod (site-packages is read-only, so `--user` install)
```bash
POD=$(oc get pods -n aap -o name | grep automationmetricsservice-web | head -1); POD=${POD#pod/}
oc exec -n aap "$POD" -c metrics-web -- python3.12 -m pip install --user psutil
```

2. Run the benchmark

The pod's own Django settings already have an `awx` DB alias pre-configured (pointing at the
same AWX/controller DB `fill_data.py` filled, via a read-only user) — no `DB_NAME` or DB env
vars need to be set, the script's defaults just work. Only `TEST_SINCE`/`TEST_UNTIL` need to
match the range you used with `fill_data.py`. `INCREMENT_HOURS` (default `6`) and
`INCREMENT_COUNT` (default `4`) don't need to be set either, same as in
[BENCHMARK_JENKINS_PODMAN.md](./BENCHMARK_JENKINS_PODMAN.md) — only override them if you want
different incremental windows.
```bash
oc exec -n aap "$POD" -c metrics-web -- sh -c '
export TEST_SINCE=2026-04-25
export TEST_UNTIL=2026-07-24
export PYTHONPATH=/tmp:$HOME/.local/lib/python3.12/site-packages
python3.12 /tmp/benchmark_dashboard_collection.py
' | tee results_openshift/results_scale1_internal_openshift.txt
```
`PYTHONPATH` must include both `/tmp` (where the script was copied) and the `--user` install
site-packages dir (where `psutil` landed), since neither is on the default path.

> Sanity-check with a short range first (e.g. `TEST_SINCE=2026-04-25 TEST_UNTIL=2026-04-26`)
> before running the full period — same idea as testing `fill_data.py` with a 1-day range first.

### API benchmarks
> **Important**: API tests require collection tests to run first!

Get the `BENCHMARK_USER`:
```bash
oc exec -n aap "$POD" -c metrics-web -- sh -c '
python3.12 -c "
import django, os
os.environ.setdefault(\"DJANGO_SETTINGS_MODULE\", \"metrics_service.settings\")
django.setup()
from apps.core.models import User
for u in User.objects.filter(is_superuser=True):
    print(u.username)
"
'
```


#### Run benchmark_dashboard_api.py
```bash
oc exec -n aap "$POD" -c metrics-web -- sh -c '
export DIRECT_DB=true
export BENCHMARK_USER=admin
export TEST_SINCE=2026-04-25
export TEST_UNTIL=2026-07-24
export PYTHONPATH=/tmp:$HOME/.local/lib/python3.12/site-packages
python3.12 /tmp/benchmark_dashboard_api.py
' | tee results_openshift/results_scale1_api_openshift.txt
```


#### Run benchmark_dashboard_api_slo.py
```bash
oc exec -n aap "$POD" -c metrics-web -- sh -c '
export DIRECT_DB=true
export BENCHMARK_USER=admin
export TEST_SINCE=2026-04-25
export TEST_UNTIL=2026-07-24
export PYTHONPATH=/tmp:$HOME/.local/lib/python3.12/site-packages
python3.12 /tmp/benchmark_dashboard_api_slo.py
' | tee results_openshift/results_scale1_api_slo_openshift.txt
```
