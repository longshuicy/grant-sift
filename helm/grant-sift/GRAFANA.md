# Grafana for Grant Sift

Grafana OSS in the **same `grant-sift` namespace**, charts from SQLite rollups — **no Prometheus**.

```
run.py daily  (GRANT_SIFT_DAILY_AT, in-app — not a k8s CronJob)
    → telemetry_daily rows in grant-sift.db
         → GET http://grant-sift:8080/api/stats
              → Grafana Infinity datasource
```

`opportunities.json` stays UI-only. Do not point Grafana at the SQLite PVC.

Parent tracking: [#3](https://github.com/longshuicy/grant-sift/issues/3) / [#14](https://github.com/longshuicy/grant-sift/issues/14).

---

## Enable / install

Chart dependency: official [`grafana/grafana`](https://artifacthub.io/packages/helm/grafana/grafana) (OSS). Gated by `grafana.enabled`.

### 1. Admin secret (software-dev)

```bash
GRAFANA_PW="$(openssl rand -base64 24)"
kubectl -n grant-sift create secret generic grant-sift-grafana \
  --from-literal=admin-user=admin \
  --from-literal=admin-password="$GRAFANA_PW"
echo "Save this password: $GRAFANA_PW"
```

`values-software-dev.yaml` already sets `grafana.enabled: true` and
`grafana.admin.existingSecret: grant-sift-grafana`.

### 2. Helm upgrade

```bash
cd helm/grant-sift
helm dependency update
helm upgrade --install grant-sift . \
  -n grant-sift \
  -f values-software-dev.yaml \
  -f values-secrets.yaml
```

Watch:

```bash
kubectl -n grant-sift get pods,ingress -l 'app.kubernetes.io/name in (grafana,grant-sift)'
# or by name:
kubectl -n grant-sift get deploy,svc,ingress | grep -E 'grafana|grant-sift'
```

UI (software-dev): **https://grant-sift-grafana.software-dev.ncsa.illinois.edu**  
Login: `admin` / password from the secret above.

Port-forward if ingress is off:

```bash
kubectl -n grant-sift port-forward svc/grant-sift-grafana 3000:80
# open http://127.0.0.1:3000
```

### 3. Seed today’s rollup (once)

The nightly job writes telemetry at the **end** of `daily`. To fill Grafana before the next 06:00 run:

```bash
kubectl -n grant-sift exec deploy/grant-sift -- python run.py telemetry
curl -sS http://grant-sift:8080/api/stats | head   # from inside the cluster
# or:
kubectl -n grant-sift exec deploy/grant-sift -- \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/api/stats').read()[:500])"
```

---

## Datasource (provisioned)

Helm values install the **Infinity** plugin and provision:

| Field | Value |
|---|---|
| Name | Grant Sift Stats |
| UID | `grant-sift-stats` |
| Type | `yesoreyeram-infinity-datasource` |
| Allowed host | `http://grant-sift:8080` |

Grafana talks to the **ClusterIP** app Service (not oauth2-proxy, not the public Ingress). That keeps `/api/stats` off the public auth path for the scraper while still reachable in-cluster.

### Manual panel query

1. Explore → **Grant Sift Stats**
2. Type: **JSON**, Source: **URL**
3. URL examples:

```
http://grant-sift:8080/api/stats?metric=category_count&since_days=90
http://grant-sift:8080/api/stats?metric=assess_backlog&since_days=30
http://grant-sift:8080/api/stats?metric=feedback_down&day_from=2026-08-01
```

4. Root / rows: `rows`
5. Columns: `day` (Time), `value` (Number), optional `dim` (String) for series split

---

## `/api/stats` shape

```json
{
  "generated_at": "2026-09-13T18:00:00+00:00",
  "metrics": ["assess_backlog", "category_count", "..."],
  "count": 42,
  "rows": [
    {"day": "2026-09-13", "metric": "category_count", "dim": "embedded_software", "value": 17, "recorded_at": "..."}
  ]
}
```

| Query param | Meaning |
|---|---|
| `metric` | Filter to one metric name |
| `since_days` | Rolling window (default **90**; ignored if `day_from` / `day_to` set) |
| `day_from` / `day_to` | Inclusive `YYYY-MM-DD` range |

### Metrics (v1)

**Ops (stock unless noted)**

| Metric | `dim` | Notes |
|---|---|---|
| `opps_total` | — | Opportunities stored |
| `assessed_total` | — | Assessments present |
| `assess_backlog` | — | opps − assessed |
| `opps_new` | — | **Flow** — `first_seen` that calendar day |
| `source_yield` | source name | Last ingest yield |
| `source_zero_streak` | source name | Consecutive empty yields |
| `detail_cache_ok` / `detail_cache_fail` | — | Detail cache health |
| `subscribers` | — | Digest subscriber rows |

**Signal / ranking**

| Metric | `dim` | Notes |
|---|---|---|
| `category_count` | category | Assessment mix (stock) |
| `score_band` | `0-39` … `80-100` | Score distribution (stock) |
| `roster_match_count` / `roster_match_rate` | — | Non-empty `match_name` |
| `feedback_up` / `feedback_down` | aspect | **Flow** that day |
| `digest_sent` | feed | **Flow** from `sent_log` |

Stock metrics are end-of-day snapshots; flow metrics count events on that day (`GRANT_SIFT_DAILY_TZ`).

---

## Starter dashboard

ConfigMap `grant-sift-grafana-dashboards` (label `grafana_dashboard=1`) ships **Grant Sift — Ops & Signal** (`uid: grant-sift-ops-signal`): backlog, new opps, category mix, score bands, feedback-down, source zero-streak.

Edit freely in the UI; persistence PVC keeps local changes. To update the shipped JSON, edit `helm/grant-sift/dashboards/grant-sift-ops-signal.json` and helm upgrade.

---

## Disable Grafana

```yaml
grafana:
  enabled: false
```

Or omit the overlay block. Re-run `helm upgrade`.

---

## Security notes

- Grafana admin password lives in Secret `grant-sift-grafana` — not in git.
- Ingress TLS is on; Grafana’s own login is the gate (not Keycloak in v1). Tighten later with oauth if needed.
- `/api/stats` is unauthenticated on the app (same idea as `/api/health`). Prefer ClusterIP-only scrapes; do not publish a public Ingress that bypasses oauth2-proxy just for stats.
