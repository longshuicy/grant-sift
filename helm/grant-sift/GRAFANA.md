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

### 1. Keycloak client (same realm as the dashboard)

Use the **same** Keycloak realm as oauth2-proxy (`keycloak.realm`, default **NCSA**). Create a **separate** confidential client for Grafana (cleaner redirect URIs than reusing `grant-sift`).

In [Keycloak admin](https://keycloak.software-dev.ncsa.illinois.edu/) → realm **NCSA** → Clients → Create:

| Field | Value |
|---|---|
| Client ID | `grant-sift-grafana` |
| Client authentication | **On** (confidential) |
| Standard flow | On |
| Valid redirect URIs | `https://grant-sift-grafana.software-dev.ncsa.illinois.edu/login/generic_oauth` |
| Valid post logout redirect URIs | `https://grant-sift-grafana.software-dev.ncsa.illinois.edu/*` |
| Web origins | `https://grant-sift-grafana.software-dev.ncsa.illinois.edu` |

Copy the **client secret** from the Credentials tab.

Issuer URLs are built from `keycloak.url` + `keycloak.realm` into ConfigMap `grant-sift-keycloak` (`auth-url`, `token-url`, `api-url`) — the same ConfigMap oauth2-proxy uses for `issuer-url`.

### 2. Admin + OIDC secret (software-dev)

```bash
GRAFANA_PW="$(openssl rand -base64 24)"
kubectl -n grant-sift create secret generic grant-sift-grafana \
  --from-literal=admin-user=admin \
  --from-literal=admin-password="$GRAFANA_PW" \
  --from-literal=client-secret='PASTE_KEYCLOAK_GRAFANA_CLIENT_SECRET'
echo "Save break-glass admin password: $GRAFANA_PW"
```

`values-software-dev.yaml` sets:

- `grafana.enabled: true`
- `grafana.keycloakAuth.enabled: true`
- Generic OAuth → Keycloak via `GF_AUTH_GENERIC_OAUTH_*` env from ConfigMap + `client-secret`
- `grafana.admin.existingSecret: grant-sift-grafana` (break-glass local admin still works)

Update the secret later:

```bash
kubectl -n grant-sift create secret generic grant-sift-grafana \
  --from-literal=admin-user=admin \
  --from-literal=admin-password="$GRAFANA_PW" \
  --from-literal=client-secret='...' \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n grant-sift rollout restart deploy/grant-sift-grafana
```

### 3. Helm upgrade

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

- Prefer **Sign in with Keycloak** (same NCSA realm as the grant-sift dashboard).
- Local `admin` / secret password remains as break-glass (`auth.disable_login_form: false`).
- **Anonymous Viewer** is on for software-dev so the Ops & Signal board can be linked
  and **embedded** in the grant-sift app without a Grafana login. Edits still need
  Keycloak or admin.

Port-forward if ingress is off:

```bash
kubectl -n grant-sift port-forward svc/grant-sift-grafana 3000:80
# open http://127.0.0.1:3000
```

### 4. Seed today’s rollup (once)

The nightly job writes telemetry at the **end** of `daily`. To fill Grafana before the next 06:00 run:

```bash
kubectl -n grant-sift exec deploy/grant-sift -- python run.py telemetry
curl -sS http://grant-sift:8080/api/stats | head   # from inside the cluster
# or:
kubectl -n grant-sift exec deploy/grant-sift -- \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/api/stats').read()[:500])"
```

---

## Auth map (Keycloak)

| Piece | Where |
|---|---|
| Realm | `keycloak.realm` (same as oauth2-proxy) |
| Issuer / OIDC URLs | ConfigMap `grant-sift-keycloak` |
| Grafana client id | `grant-sift-grafana` (`grafana.ini` + `keycloakAuth.clientId`) |
| Grafana client secret | Secret `grant-sift-grafana` key `client-secret` |
| Dashboard (app) client | Still `grant-sift` via oauth2-proxy |

New users who sign in with Keycloak get org role **Editor** (`users.auto_assign_org_role`). Tighten with `role_attribute_path` later if you add Keycloak roles.

### Public view + app link

software-dev enables **anonymous Viewer** so the Ops & Signal board can be opened
without a Grafana login. The grant-sift header shows a quiet **Ops & signal** link under the brand title
when `GRANT_SIFT_GRAFANA_URL` is set — text link only, no iframe, not a fourth stat.

```
https://grant-sift-grafana.software-dev.ncsa.illinois.edu/d/grant-sift-ops-signal?orgId=1&from=now-90d&to=now&theme=light&refresh=5m
```

Local:

```bash
export GRANT_SIFT_GRAFANA_URL='https://grant-sift-grafana.software-dev.ncsa.illinois.edu/d/grant-sift-ops-signal?orgId=1&from=now-90d&to=now&theme=light&refresh=5m'
python run.py serve
```

If the Grafana UI shows “failed to load its application files”, check `server.root_url` (trailing `/`) and Traefik TLS — that is **not** caused by empty `telemetry_daily` rows. Empty telemetry only means blank panels after Grafana loads.

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

- Grafana admin password + Keycloak client secret live in Secret `grant-sift-grafana` — not in git.
- Prefer Keycloak (same NCSA realm as the app). Local admin is break-glass only.
- Anonymous **Viewer** is intentional on software-dev for a public board link; turn it off if the host must not be world-readable.
- Ingress TLS is on. `/api/stats` stays unauthenticated on the app ClusterIP (same idea as `/api/health`); Grafana scrapes in-cluster, not via the public oauth2 Ingress.
- Do not publish a public Ingress that bypasses oauth2-proxy just for stats.
