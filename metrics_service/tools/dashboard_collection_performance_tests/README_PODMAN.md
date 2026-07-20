# Dashboard Collection Performance Tests — Podman / Jenkins

This is the **Podman/Jenkins** counterpart to `README.md`. `README.md` covers
running these benchmarks locally against a `docker-compose`-managed dev
environment; this file covers running the same scripts on a **Jenkins AIO
instance** where metrics-service runs as a **Podman container** in
**production mode**.

Production mode changes things: the HTTP API only accepts a JWT from the AAP
gateway, so plain `BENCHMARK_USER`/`PASSWORD` Basic Auth (as used locally)
gets rejected with `401`/`403`. Every script below has a `DIRECT_DB=true`
mode that bypasses HTTP entirely and drives the same code in-process via the
ORM/DRF test client, without needing to modify the running container. Set it
whenever running against the Jenkins instance.

| Script | What it measures | Podman/Jenkins doc |
|--------|-----------------|---------------------|
| `fill_data.py` | Populates the AWX database with synthetic test data | — (see below) |
| `benchmark_dashboard_collection.py` | Internal benchmark — calls `_collect_data()` directly in Python | [`BENCHMARK_JENKINS_PODMAN.md`](./BENCHMARK_JENKINS_PODMAN.md) |
| `benchmark_dashboard_api.py` | API benchmark — triggers a collection task and polls it to completion | [`BENCHMARK_JENKINS_PODMAN_API.md`](./BENCHMARK_JENKINS_PODMAN_API.md) |
| `benchmark_dashboard_api_slo.py` | SLO benchmark — times 11 `dashboard_reports` read endpoints against already-collected data | [`BENCHMARK_JENKINS_PODMAN_API_SLO.md`](./BENCHMARK_JENKINS_PODMAN_API_SLO.md) |

For full one-time setup (copying scripts into the container, installing
`psutil`, placing `mock_awx` in site-packages), prerequisites, troubleshooting
tables, and copy-pasteable commands, see the linked doc for each script — this
README only gives the overview and the fast path.

---

## Environment

- Jenkins AIO instance, reachable over SSH
- metrics-service running as the `automation-metrics-web` Podman container, in **production mode**
- AWX running as its own container(s), with the AWX DB reachable from `automation-metrics-web`
- `metrics-utility` and `metrics-service` checked out on the instance (paths used throughout: `~/aap/metrics-utility`, `~/aap/metrics-service`)

Everything runs via `podman exec` against `automation-metrics-web` — there's
no local venv, no `docker-compose up`, no separate web/dispatcherd terminals.

---

## 1 — Filling test data (`fill_data.py`)

Same script and scale presets (1–4) as the local workflow — see `README.md`
for the scale reference table and full argument list. The only difference on
Jenkins is that it's invoked via `podman exec` with `METRICS_UTILITY_DB_*`
env vars pointing at the AWX DB, and the AWX `main_activitystream_*` tables
must be truncated first (FK constraints block `clean_all_data.py`). Both
steps are shown in each `BENCHMARK_JENKINS_PODMAN*.md` doc.

---

## 2 — Internal benchmark (`benchmark_dashboard_collection.py`)

Calls `_collect_data()` directly in Python, in-process inside the container
— no HTTP involved, so no auth workaround needed here. Same two phases
(initial backfill + incremental sync) as local. Run via:

```bash
podman exec automation-metrics-web python3.12 manage.py shell -c "
exec(open('/tmp/benchmark_dashboard_collection.py').read())
run_dashboard_collection_benchmark()
"
```

Full setup and the truncate → fill → benchmark loop for all 4 scales:
see [`BENCHMARK_JENKINS_PODMAN.md`](./BENCHMARK_JENKINS_PODMAN.md), or run
`./run_all_collection_bench.sh` (`AWX_DB_PASSWORD=... ./run_all_collection_bench.sh`).

---

## 3 — API benchmark (`benchmark_dashboard_api.py`)

Triggers `collect_dashboard_reports_data` and polls until completion, then
times a CSV export — same three phases (month/week/day) as local. On
Jenkins, set `DIRECT_DB=true` so `trigger_task`, `wait_for_task`,
`export_csv`, and the connectivity check all go through the ORM/DRF test
client instead of real HTTP:

```bash
podman exec automation-metrics-web bash -c "
  DIRECT_DB=true \
  BENCHMARK_USER=admin \
  python3.12 manage.py shell -c \"
exec(open('/tmp/benchmark_dashboard_api.py').read())
main()
\"
"
```

Full setup and the truncate → fill → benchmark loop for all 4 scales:
see [`BENCHMARK_JENKINS_PODMAN_API.md`](./BENCHMARK_JENKINS_PODMAN_API.md), or
run `./run_all_api_bench.sh` / `./run_all_bench.sh` (the latter also runs the internal
benchmark in the same pass — see its `--help`).

---

## 4 — SLO benchmark (`benchmark_dashboard_api_slo.py`)

Doesn't trigger any collection — times 11 `dashboard_reports` read endpoints
(report, report details, CSV export, templates, filter sets, subscription
costs, labels, organizations, projects, collection status, collection
telemetry) against whatever data is already in the DB, once per phase window.
Use it to check read-side response times independently of collection
performance; requires data to already be present (run `fill_data.py` +
one of the collection benchmarks above first). Same `DIRECT_DB=true` fix
applies here too, via a shared `_get()` helper used by every endpoint timer:

```bash
podman exec automation-metrics-web bash -c "
  DIRECT_DB=true \
  BENCHMARK_USER=admin \
  python3.12 manage.py shell -c \"
exec(open('/tmp/benchmark_dashboard_api_slo.py').read())
main()
\"
"
```

Full setup and details: see [`BENCHMARK_JENKINS_PODMAN_API_SLO.md`](./BENCHMARK_JENKINS_PODMAN_API_SLO.md).

---

## Automation scripts

- `run_all_collection_bench.sh` — truncate → fill → internal benchmark, for scales 1–4
- `run_all_api_bench.sh` — truncate → fill → API benchmark, for scales 1–4
- `run_all_bench.sh` — parameterized version covering both the internal and API
  benchmarks per scale in one pass (`--user`, `--awx-db-*`, `--scales`,
  `--test-since`/`--test-until`, `--skip-collection`/`--skip-api`, etc.);
  run `./run_all_bench.sh --help` for the full option list

None of these currently drive `benchmark_dashboard_api_slo.py` — run it
separately per [`BENCHMARK_JENKINS_PODMAN_API_SLO.md`](./BENCHMARK_JENKINS_PODMAN_API_SLO.md)
once data has been collected.

---

## Troubleshooting

Each `BENCHMARK_JENKINS_PODMAN*.md` doc has its own troubleshooting table.
Common cross-cutting issues:

- **`{"detail":"Authentication credentials were not provided."}`** — production mode requires a JWT; set `DIRECT_DB=true` instead of Basic Auth
- **Env vars set via `podman exec -e` don't seem to apply to the web server** — `podman exec` only sets env for the new process it spawns (e.g. the `manage.py shell` you're running); the actual gunicorn/nginx web process was already started with its own env at container boot. Settings baked into Dynaconf at process start (like `REST_FRAMEWORK__DEFAULT_AUTHENTICATION_CLASSES` or `CSRF_TRUSTED_ORIGINS`) require changing the container's launch config (compose file / supervisord env / installer inventory) and restarting the container — not just `podman exec -e`. `DIRECT_DB=true` avoids this entirely since it never goes through the web server.
- **Results not found on your machine** — result files are written to `/home/ansible/` (or your `--results-dir`) on the Jenkins host; `scp` them back, see each doc's "Collecting results" section
