# Analytics Ingress

External collection pipelines should target the `AnalyticsPayload` model (the
default Django table is `analytics_analyticspayload`) when storing non-local
analytics payloads. Set `source` to the originating component instead of the
default `local` value.

Collector names may be namespaced by source, for example
`controller.main_host` and `eda.main_host`. The current analytics API is not
source-aware: its collector rows endpoint filters by `collector` only. Avoid
colliding names, or expect rows from the same-named collectors to share one API
view and its overlap semantics.

Field semantics are:

- `collector`: public collector name used by the API path and row lookup.
- `source`: originating component, such as `local`, `controller`, `eda`, or `hub`.
- `since`: inclusive collection-window start; nullable for open or snapshot data.
- `until`: exclusive collection-window end; nullable for open or snapshot data.
- `started_at` / `finished_at`: collection execution timestamps.
- `payload`: raw JSON collector output.
- `created` / `modified` and `created_by` / `modified_by`: DAB audit fields.

Rows are append-only from the collection API perspective: repeated windows and
snapshots are retained. Ingress writers should preserve the same field semantics
and avoid using `source` as a substitute for a separate API filter.
