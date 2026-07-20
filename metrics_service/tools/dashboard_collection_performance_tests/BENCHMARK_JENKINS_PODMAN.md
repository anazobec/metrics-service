# Dashboard Collection Performance Tests — Jenkins (Podman AIO)

How to run `benchmark_dashboard_collection.py` on a Jenkins AIO instance
where metrics-service runs as a Podman container.

## Prerequisites

- SSH access to the Jenkins instance
- Both repos cloned locally (onto the instance):
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

# dashboard collection benchmark scripts
podman cp ~/aap/metrics-service/metrics_service/tools/dashboard_collection_performance_tests/fill_data.py \
    automation-metrics-web:/tmp/fill_data.py
podman cp ~/aap/metrics-service/metrics_service/tools/dashboard_collection_performance_tests/benchmark_dashboard_collection.py \
    automation-metrics-web:/tmp/benchmark_dashboard_collection.py
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

## Running the benchmarks

For each scale: **truncate activitystream tables → fill → benchmark**.

### Truncate activitystream tables (required before each scale to get a clean AWX DB)

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

### Run the benchmark

```bash
podman exec automation-metrics-web \
  -e PYTHONPATH=/tmp
  python3.12 manage.py shell -c "
exec(open('/tmp/benchmark_dashboard_collection.py').read())
run_dashboard_collection_benchmark()
" \
  | tee /home/ansible/results_scale<N>_internal.txt
```

---

## Full example — all 4 scales

```bash
cd ~/aap/metrics-service/metrics_service/tools/dashboard_collection_performance_tests
chmod +x run_collection_bench.sh

# run benchmark on all 4 scales
./run_collection_bench.sh
```

---

## Collecting results

All result files are written to `/home/ansible/` on the Jenkins host. SCP them back:

```bash
scp ansible@<jenkins-ip>:/home/ansible/results_scale*_internal.txt ./
```

---

## Troubleshooting

| Error | Fix |
|-------|-----|
| `No module named 'awx'` | `mock_awx` is not in site-packages — re-run the copy step in One-time setup |
| `No module named 'modules'` | `modules.py` missing from `/tmp` — re-run the `podman cp` for `anonymized_db_perf_data/.` |
| `No module named 'psutil'` | Run `podman exec automation-metrics-web pip3.12 install psutil` |
| `ForeignKeyViolation` on fill | Truncate the `main_activitystream_*` tables first (see above) |
| `FileNotFoundError: /tmp/benchmark_dashboard_collection.py` | File is on the host `/tmp`, not the container — re-run the `podman cp` step |
| `run_dashboard_collection_benchmark` only prints one line | You ran the script without calling the function — use the `exec(...)` + function call form shown above |
| `scripts directory not found` | `METRICS_UTILITY_PATH=/tmp/mu` is not set or `/tmp/mu/tools/anonymized_db_perf_data/` was not created |

---

## Security notes

- **DB password:** pass `AWX_DB_PASSWORD` via `-e` on `podman exec` (the run_*.sh scripts do this) — never inline it into a `bash -c "..."` string, which would leak it in `ps aux` output on the host/container.
- **`/tmp` integrity:** files copied to the container's `/tmp` are exec()'d by the run_*.sh scripts across multiple scale iterations. `/tmp` is writable by anyone with `podman exec` access to the container, so the run scripts pin a sha256 checksum at start and re-verify it before each exec — if it changes mid-run (tampering, a stray process, etc.), the script aborts instead of running unexpected code.
- **`DIRECT_DB=false` + plain HTTP:** the benchmark scripts warn at startup if `BASE_URL` is non-HTTPS and non-loopback, since `HTTPBasicAuth` sends credentials base64-encoded (not encrypted) over the wire in that mode. Prefer `DIRECT_DB=true` or HTTPS when testing against a non-local target.
