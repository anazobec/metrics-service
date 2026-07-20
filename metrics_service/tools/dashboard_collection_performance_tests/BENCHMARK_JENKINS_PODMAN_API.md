# Dashboard Collection Performance Tests — API Benchmark (Jenkins / Podman AIO)

How to run `benchmark_dashboard_api.py` on a Jenkins AIO instance
where metrics-service runs as a Podman container.

This is the **API variant** of the benchmark. Unlike `benchmark_dashboard_collection.py`
which calls the collector directly in-process, this script triggers tasks and polls
for completion — measuring actual `started_at → completed_at` execution time as seen
from the outside.

> **Note on the production HTTP API:** In production mode the
> `/api/metrics/v1/tasks/` endpoint requires JWT authentication from the AAP
> gateway — Basic Auth is not accepted. To work around this without modifying
> the running container, the benchmark overrides `trigger_task` and
> `wait_for_task` with direct DB equivalents and mocks the connectivity check.
> The measured times are identical to the HTTP path.

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

# API benchmark script
podman cp ~/aap/metrics-service/metrics_service/tools/dashboard_collection_performance_tests/benchmark_dashboard_api.py \
    automation-metrics-web:/tmp/benchmark_dashboard_api.py

# prometheus_utils.py (shared Prometheus helpers, required import)
podman cp ~/aap/metrics-service/metrics_service/tools/dashboard_collection_performance_tests/prometheus_utils.py \
    automation-metrics-web:/tmp/prometheus_utils.py

# fill_data.py (shared with internal benchmark)
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

`benchmark_dashboard_api.py` normally uses `HTTPBasicAuth` to call
`POST /api/metrics/v1/tasks/schedule_immediate/` and `GET /api/metrics/v1/tasks/{id}/`.
In production mode that endpoint requires a JWT token from the AAP gateway — Basic
Auth returns `401`/`403`. Set `DIRECT_DB=true` and the script itself swaps in
direct DB/ORM equivalents — no monkeypatching needed at the call site:

| Original (HTTP) | `DIRECT_DB=true` replacement |
|----------|-------------|
| `trigger_task()` — HTTP POST to `/tasks/schedule_immediate/` | Creates a `Task` row directly in the metrics-service DB |
| `wait_for_task()` — HTTP GET polls `/tasks/{id}/` | Polls `Task.objects.get(id=task_id).status` directly |
| `export_csv()` — HTTP GET to `/dashboard_reports/report/export/` | Calls the same view in-process via DRF's `APIClient.force_authenticate()` |
| Connectivity check in `main()` — `requests.get(BASE_URL/v1/)` | Checks the benchmark user exists in the DB |

Execution time is still measured the same way (`completed_at - started_at` on
the `Task` record, wall-clock around the export view call) — the same value
the HTTP path would return, since it's the exact same underlying code path.

> Earlier versions of this doc achieved the same thing by monkeypatching
> `trigger_task`/`wait_for_task` and mocking `requests.get` inline in the
> `manage.py shell -c` command. That approach missed `export_csv()`, which
> still made a real (unauthenticated) HTTP call and silently failed with
> "Failed to download CSV" / an empty result. `DIRECT_DB=true` now covers
> all three HTTP call sites consistently.

---

## Running the benchmarks

For each scale: **truncate activitystream tables → fill → benchmark**.

### Truncate activitystream tables (required before each scale)

The AWX DB has FK constraints on `main_activitystream_*` junction tables that
block `clean_all_data.py`. Truncate them first:

```bash
podman exec postgresql psql -U awx -d awx -c "
TRUNCATE TABLE
  main_activitystream_job_template,
  main_activitystream_organization,
  main_activitystream_credential,
  main_activitystream_execution_environment,
  main_activitystream_host;
"
```

> If this errors, add `CASCADE` to the `TRUNCATE` statement.

### Fill the AWX DB

```bash
podman exec automation-metrics-web bash -c "
  METRICS_UTILITY_PATH=/tmp/mu \
  METRICS_UTILITY_DB_HOST=aio-0 \
  METRICS_UTILITY_DB_PORT=5432 \
  METRICS_UTILITY_DB_NAME=awx \
  METRICS_UTILITY_DB_USER=awx \
  METRICS_UTILITY_DB_PASSWORD='<awx-db-password>' \
  python3.12 /tmp/fill_data.py --scale <N>
"
```

Replace `<N>` with `1`, `2`, `3`, or `4`. Scale reference:

| Scale | Jobs/day | Hosts | `main_unifiedjob` | `main_jobhostsummary` |
|------:|---------:|------:|------------------:|---------------------:|
| 1     | 100      | 5     | ~9,100            | ~45,500              |
| 2     | 500      | 5     | ~45,500           | ~227,500             |
| 3     | 1,100    | 5     | ~100,100          | ~500,500             |
| 4     | 500      | 50    | ~45,500           | ~2,275,000           |

### Run the API benchmark

```bash
podman exec automation-metrics-web bash -c "
  METRICS_SERVICE_LOG_LEVEL=WARNING \
  METRICS_SERVICE_FEATURE__DASHBOARD_COLLECTION=true \
  DIRECT_DB=true \
  BENCHMARK_USER=admin \
  PYTHONPATH=/tmp \
  TEST_SINCE=since_date \  # until_date - 90_days
  TEST_UNTIL=until_date \  # should be your current date
  python3.12 manage.py shell -c \"
exec(open('/tmp/benchmark_dashboard_api.py').read())
main()
\"
" | tee /home/ansible/results_scale<N>_api.txt
```

> **Help**:
> - `since_date` is `until_date - 90_days`
> - `until_date` should be your current date

---

## Full example — all 4 scales


```bash
cd ~/aap/metrics-service/metrics_service/tools/dashboard_collection_performance_tests
chmod +x run_api_bench.sh

# run benchmark on all 4 scales
./run_api_bench.sh
```

---

## Collecting results

All result files are written to `/home/ansible/` on the Jenkins host. SCP them back:

```bash
scp ansible@<jenkins-ip>:/home/ansible/results_scale*_api.txt ./
```

---

## Troubleshooting

| Error | Fix |
|-------|-----|
| `No module named 'awx'` | `mock_awx` is not in site-packages — re-run the copy step in One-time setup |
| `No module named 'modules'` | `modules.py` missing from `/tmp` — re-run the `podman cp` for `anonymized_db_perf_data/.` |
| `No module named 'psutil'` | Run `podman exec automation-metrics-web pip3.12 install psutil` |
| `ForeignKeyViolation` on fill | Truncate the `main_activitystream_*` tables first (see above) |
| `FileNotFoundError: /tmp/benchmark_dashboard_api.py` | File is on the host `/tmp`, not the container — re-run the `podman cp` step |
| `ModuleNotFoundError: No module named 'prometheus_utils'` | `prometheus_utils.py` missing from `/tmp` — re-run the `podman cp` for it |
| `{"detail":"Authentication credentials were not provided."}` | The tasks endpoint requires JWT in production mode — set `DIRECT_DB=true`, do not attempt Basic Auth |
| `{"detail":"This endpoint is only available when development mode is enabled."}` | Same root cause — production mode restricts the tasks HTTP API. Set `DIRECT_DB=true` |
| `Failed to download CSV: 403 Client Error...` | `export_csv()` also hits HTTP and needs `DIRECT_DB=true` — older doc versions only patched `trigger_task`/`wait_for_task` and missed this call site |
| `Connection refused` on port 18002 or similar | `BASE_URL` defaulted from the script env — harmless when `DIRECT_DB=true` since no HTTP call is made |
| `GetPassWarning: Can not control echo on the terminal` | Add `-it` to `podman exec` when running `changepassword` interactively |
| `python3.12: command not found` after a pipe | The pipe runs on the host shell which has no `python3.12` — use `python3` on the host side or run everything inside the container with `bash -c` |

---

## Security notes

- **DB password:** pass `AWX_DB_PASSWORD` via `-e` on `podman exec` (the run_*.sh scripts do this) — never inline it into a `bash -c "..."` string, which would leak it in `ps aux` output on the host/container.
- **`/tmp` integrity:** files copied to the container's `/tmp` are exec()'d by the run_*.sh scripts across multiple scale iterations. `/tmp` is writable by anyone with `podman exec` access to the container, so the run scripts pin a sha256 checksum at start and re-verify it before each exec — if it changes mid-run (tampering, a stray process, etc.), the script aborts instead of running unexpected code.
- **`DIRECT_DB=false` + plain HTTP:** the benchmark scripts warn at startup if `BASE_URL` is non-HTTPS and non-loopback, since `HTTPBasicAuth` sends credentials base64-encoded (not encrypted) over the wire in that mode. Prefer `DIRECT_DB=true` or HTTPS when testing against a non-local target.
