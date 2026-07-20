# Dashboard Collection Performance Tests — API SLO Benchmark (Jenkins / Podman AIO)

How to run `benchmark_dashboard_api_slo.py` on a Jenkins AIO instance
where metrics-service runs as a Podman container.

This is the **SLO variant** of the API benchmark. Unlike `benchmark_dashboard_api.py`,
which triggers a `collect_dashboard_reports_data` task and times the collection
itself, this script does **not** trigger any collection task — it times a
fixed set of `dashboard_reports` read endpoints (report, report details, CSV
export, templates, filter sets, subscription costs, labels, organizations,
projects, collection status, collection telemetry) against whatever data is
already in the DB, for each of the three phase windows (month / week / day).
Use it to check response-time SLOs on the read side of the API, independent
of collection performance.

> **Note on the production HTTP API:** In production mode these endpoints
> require JWT authentication from the AAP gateway — Basic Auth is not
> accepted. Set `DIRECT_DB=true` and the script itself routes every request
> through DRF's `APIClient` with `force_authenticate()` instead of a real
> HTTP call — no monkeypatching needed, and the same view code runs either
> way, so measured durations are still comparable to the HTTP path.

---

## Prerequisites

- SSH access to the Jenkins instance
- Both repos cloned locally:
  - `metrics-utility` at `~/aap/metrics-utility`
  - `metrics-service` at `~/aap/metrics-service`
- The running container is called `automation-metrics-web`
- AWX DB credentials (write access) — find them with:
  ```bash
  podman exec automation-controller-web awx-manage print_settings DATABASES
  ```
- metrics-service DB credentials — find them with:
  ```bash
  podman exec automation-metrics-web \
    python3.12 manage.py shell -c \
    "from django.conf import settings; import json; print(json.dumps(settings.DATABASES, indent=2))"
  ```
- Data already collected in `JobData`/`TemplateMetadata` for the period you
  want to test — this benchmark reads existing data, it does not run a
  collection task. Run `fill_data.py` and either
  `benchmark_dashboard_api.py` or `benchmark_dashboard_collection.py` first
  (see `BENCHMARK_JENKINS_PODMAN_API.md` / `BENCHMARK_JENKINS_PODMAN.md`) so
  there's something to read.

> **Important**: You must first have run `benchmark_dashboard_collection.py` (see [BENCHMARK_JENKINS_PODMAN.md](BENCHMARK_JENKINS_PODMAN.md)) so that all the jobs are properly run first (sync). You **must not** truncate/delete data from the table after running the collection benchmark otherwise, you'll have no actual data to test on!

---

## One-time setup

### 1. Copy the perf test scripts into the container

```bash
# All scripts from anonymized_db_perf_data
podman cp ~/aap/metrics-utility/tools/anonymized_db_perf_data/. \
    automation-metrics-web:/tmp/

# mock_awx package — required by metrics_utility.prepare()
podman cp ~/aap/metrics-utility/mock_awx \
    automation-metrics-web:/usr/lib/python3.12/site-packages/mock_awx

# Create the directory structure fill_data.py expects at METRICS_UTILITY_PATH
podman exec automation-metrics-web mkdir -p /tmp/mu/tools/anonymized_db_perf_data
podman exec automation-metrics-web bash -c "
  cp /tmp/fill_perf_db_data.py /tmp/mu/tools/anonymized_db_perf_data/
  cp /tmp/clean_all_data.py    /tmp/mu/tools/anonymized_db_perf_data/
  cp /tmp/helpers.py           /tmp/mu/tools/anonymized_db_perf_data/
  cp /tmp/modules.py           /tmp/mu/tools/anonymized_db_perf_data/
"

# SLO API benchmark script
podman cp ~/aap/metrics-service/metrics_service/tools/dashboard_collection_performance_tests/benchmark_dashboard_api_slo.py \
    automation-metrics-web:/tmp/benchmark_dashboard_api_slo.py

# prometheus_utils.py (shared Prometheus helpers, required import)
podman cp ~/aap/metrics-service/metrics_service/tools/dashboard_collection_performance_tests/prometheus_utils.py \
    automation-metrics-web:/tmp/prometheus_utils.py

# fill_data.py (shared with the other benchmarks)
podman cp ~/aap/metrics-service/metrics_service/tools/dashboard_collection_performance_tests/fill_data.py \
    automation-metrics-web:/tmp/fill_data.py
```

### 2. Install psutil in the container

```bash
podman exec automation-metrics-web pip3.12 install psutil
```

---

## Why `mock_awx` must go into site-packages

`metrics_utility.prepare()` resolves the mock path as:
```python
os.path.join(os.path.dirname(__file__), '..', 'mock_awx')
```
`__file__` is `/usr/lib/python3.12/site-packages/metrics_utility/__init__.py`,
so it looks for `mock_awx` at `/usr/lib/python3.12/site-packages/mock_awx`.
Placing it anywhere else (e.g. `/tmp`) will not work.

---

## Why no username/password is needed

`benchmark_dashboard_api_slo.py` normally uses `HTTPBasicAuth` for every
`requests.get()` call it makes against the 11 `dashboard_reports` endpoints.
In production mode those endpoints require a JWT token from the AAP
gateway — Basic Auth returns `401`/`403`. Set `DIRECT_DB=true` and the
script itself swaps in an in-process replacement, via a shared `_get()`
helper used by every endpoint timer:

| Original (HTTP) | `DIRECT_DB=true` replacement |
|----------|-------------|
| `_get(url)` — real `requests.get(url, auth=AUTH)` | Same URL path/query, called in-process via DRF's `APIClient.force_authenticate()` |
| Connectivity check in `main()` — `requests.get(BASE_URL/v1/)` | Checks the benchmark user exists in the DB |

All 11 `time_dashboard_reports_*_endpoint()` functions
(`report`, `report/details`, CSV export, `templates`, `filter_sets`,
`subscription_costs`, `labels`, `organizations`, `projects`,
`collection_status`, `collection_telemetry`) route through this same
`_get()` helper, so `DIRECT_DB=true` fixes all of them consistently.
Execution time is still measured the same way (wall-clock around the
request) — the same value the HTTP path would return, since it's the exact
same underlying view code.

---

## Running the benchmark

Data must already exist for the period being tested (see Prerequisites) —
this script does not truncate/fill/collect anything itself, it only reads
and times the endpoints listed above.

```bash
podman exec automation-metrics-web bash -c "
  METRICS_SERVICE_LOG_LEVEL=WARNING \
  METRICS_SERVICE_FEATURE__DASHBOARD_COLLECTION=true \
  DIRECT_DB=true \
  BENCHMARK_USER=admin \
  PYTHONPATH=/tmp \
  TEST_SINCE=since_date \
  TEST_UNTIL=until_date \
  python3.12 manage.py shell -c \"
exec(open('/tmp/benchmark_dashboard_api_slo.py').read())
main()
\"
" | tee /home/ansible/results_scale<N>_api_slo.txt
```

> **Help**:
> - `since_date` is `until_date - 90_days`
> - `until_date` should be your current date

Replace `<N>` with whichever scale's data is currently loaded (see
`BENCHMARK_JENKINS_PODMAN_API.md` for the scale reference table and the
fill/truncate steps).

---

## Full example — all 4 scales

Runs truncate → fill → SLO benchmark for each scale (assumes the SLO
benchmark is the only thing you want timed; combine with the other
benchmarks' full examples if you need internal/API-task timings too):

```bash
cd ~/aap/metrics-service/metrics_service/tools/dashboard_collection_performance_tests
chmod +x run_api_slo_bench.sh

# run benchmark on all 4 scales
./run_api_slo_bench.sh
```

---

## Collecting results

All result files are written to `/home/ansible/` on the Jenkins host. SCP them back:

```bash
scp ansible@<jenkins-ip>:/home/ansible/results_scale*_api_slo.txt ./
```

---

## Troubleshooting

| Error | Fix |
|-------|-----|
| `No module named 'awx'` | `mock_awx` is not in site-packages — re-run the copy step in One-time setup |
| `No module named 'modules'` | `modules.py` missing from `/tmp` — re-run the `podman cp` for `anonymized_db_perf_data/.` |
| `No module named 'psutil'` | Run `podman exec automation-metrics-web pip3.12 install psutil` |
| `ForeignKeyViolation` on fill | Truncate the `main_activitystream_*` tables first (see above) |
| `FileNotFoundError: /tmp/benchmark_dashboard_api_slo.py` | File is on the host `/tmp`, not the container — re-run the `podman cp` step |
| `ModuleNotFoundError: No module named 'prometheus_utils'` | `prometheus_utils.py` missing from `/tmp` — re-run the `podman cp` for it |
| `{"detail":"Authentication credentials were not provided."}` | These endpoints require JWT in production mode — set `DIRECT_DB=true`, do not attempt Basic Auth |
| `{"detail":"This endpoint is only available when development mode is enabled."}` | Same root cause — production mode restricts the API. Set `DIRECT_DB=true` |
| `[Status 403] Something went wrong accessing endpoint: ...` | One of the 11 endpoint timers hit HTTP without `DIRECT_DB=true` — set it |
| Endpoint timings look artificially fast / `JobData rows in DB: 0` | No data has been collected for the tested period yet — run `fill_data.py` and a collection benchmark first (see Prerequisites) |
| `Connection refused` on port 18002 or similar | `BASE_URL` defaulted from the script env — harmless when `DIRECT_DB=true` since no HTTP call is made |
| `GetPassWarning: Can not control echo on the terminal` | Add `-it` to `podman exec` when running `changepassword` interactively |
| `python3.12: command not found` after a pipe | The pipe runs on the host shell which has no `python3.12` — use `python3` on the host side or run everything inside the container with `bash -c` |

---

## Security notes

- **DB password:** pass `AWX_DB_PASSWORD` via `-e` on `podman exec` (the run_*.sh scripts do this) — never inline it into a `bash -c "..."` string, which would leak it in `ps aux` output on the host/container.
- **`/tmp` integrity:** files copied to the container's `/tmp` are exec()'d by the run_*.sh scripts across multiple scale iterations. `/tmp` is writable by anyone with `podman exec` access to the container, so the run scripts pin a sha256 checksum at start and re-verify it before each exec — if it changes mid-run (tampering, a stray process, etc.), the script aborts instead of running unexpected code.
- **`DIRECT_DB=false` + plain HTTP:** the benchmark scripts warn at startup if `BASE_URL` is non-HTTPS and non-loopback, since `HTTPBasicAuth` sends credentials base64-encoded (not encrypted) over the wire in that mode. Prefer `DIRECT_DB=true` or HTTPS when testing against a non-local target.
