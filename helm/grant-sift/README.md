# Helm chart for Grant Sift

Deploys the dashboard (`run.py serve`, which also runs the nightly pipeline in-process), a **PVC** for SQLite, optional **Grafana OSS** (stats from `/api/stats`), and **[oauth2-proxy](https://github.com/oauth2-proxy/manifests)** in front of Keycloak.

Not Argo-managed yet — hand-roll with `helm upgrade --install`. Cluster overlay: `values-software-dev.yaml`.

SQLite is a file on the PVC (`/data/grant-sift.db`); there is no separate SQLite chart.

## Layout

| Resource | Purpose |
|---|---|
| Deployment + Service | Web UI, feedback, chat proxy, `/api/stats` (ClusterIP only) |
| oauth2-proxy + Ingress | Traefik → Keycloak login → app |
| Grafana (optional) | OSS charts for `telemetry_daily`; Infinity → `http://grant-sift:8080/api/stats` |
| PVC (`nfs-taiga`) | `/data`: `grant-sift.db` + `opportunities.json`, written by the Deployment only |
| Secret | `GRANT_SIFT_LLM_API_KEY` (pipeline) + `grant-sift-oauth2` (OIDC) + `grant-sift-grafana` (admin) |
| ConfigMap | Non-secret env + mounted `roster.yaml` + Grafana dashboards |

Grafana setup details: **[GRAFANA.md](./GRAFANA.md)**.

## Prerequisites (software-dev)

| Thing | Value |
|---|---|
| kubectl context | `software-dev` (k3s **1.34**) |
| Helm | **3.9+** |
| Ingress | **traefik** + letsencrypt |
| Storage | **nfs-taiga** (RWX) |
| DNS | `*.software-dev.ncsa.illinois.edu` |
| Keycloak | [keycloak.software-dev…](https://keycloak.software-dev.ncsa.illinois.edu/) — set `keycloak.realm` (default **NCSA**) |
| Image | **public** `ghcr.io/longshuicy/grant-sift:main` |

Traffic path:

```
Browser → Traefik → oauth2-proxy → Keycloak (NCSA) → grant-sift:8080
```

---

## Step-by-step (hand-roll)

### 0. Context

```bash
kubectl config use-context software-dev
```

### 1. Image (public)

```bash
docker pull ghcr.io/longshuicy/grant-sift:main
```

CI tags on merge to `main`: `:main`, `:sha-<short>`. Releases add semver + `:latest`. No pull secret needed.

### 2. Keycloak client

In [Keycloak admin](https://keycloak.software-dev.ncsa.illinois.edu/) → realm matching `keycloak.realm` in `values-software-dev.yaml` (default **NCSA**) → Clients → Create:

| Field | Value |
|---|---|
| Client ID | `grant-sift` (or your choice) |
| Client authentication | **On** (confidential) |
| Valid redirect URIs | `https://grant-sift.software-dev.ncsa.illinois.edu/oauth2/callback` |
| Web origins | `https://grant-sift.software-dev.ncsa.illinois.edu` |
| Standard flow | On |

Copy the **client secret** from the Credentials tab.

Realm is a Helm value (not hard-coded in the issuer URL):

```yaml
keycloak:
  url: https://keycloak.software-dev.ncsa.illinois.edu
  realm: NCSA          # ← change here if needed
```

The chart builds `{{url}}/realms/{{realm}}` into ConfigMap `grant-sift-keycloak` and injects it as `OAUTH2_PROXY_OIDC_ISSUER_URL`.

### 3. Namespace, secrets, roster

```bash
kubectl create namespace grant-sift

# Pipeline LLM key (gitignored)
cp helm/grant-sift/values-secrets.example.yaml helm/grant-sift/values-secrets.yaml
# edit: secrets.GRANT_SIFT_LLM_API_KEY: "sk_..."

# oauth2-proxy ↔ Keycloak
COOKIE_SECRET="$(python3 -c 'import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')"
kubectl -n grant-sift create secret generic grant-sift-oauth2 \
  --from-literal=client-id='grant-sift' \
  --from-literal=client-secret='PASTE_KEYCLOAK_CLIENT_SECRET' \
  --from-literal=cookie-secret="$COOKIE_SECRET"

# Collaborator roster (gitignored — not in the image)
kubectl -n grant-sift create configmap grant-sift-roster \
  --from-file=roster.yaml=config/roster.yaml \
  --from-file=ncsa_staff.yaml=config/ncsa_staff.yaml

# Grafana admin (when grafana.enabled — software-dev turns it on)
GRAFANA_PW="$(openssl rand -base64 24)"
kubectl -n grant-sift create secret generic grant-sift-grafana \
  --from-literal=admin-user=admin \
  --from-literal=admin-password="$GRAFANA_PW" \
  --from-literal=client-secret='PASTE_KEYCLOAK_GRAFANA_CLIENT_SECRET'
echo "Grafana break-glass admin password: $GRAFANA_PW"
# Keycloak client grant-sift-grafana: see GRAFANA.md (same realm as the app)
```

Update roster later:

```bash
kubectl -n grant-sift create configmap grant-sift-roster \
  --from-file=roster.yaml=config/roster.yaml \
  --from-file=ncsa_staff.yaml=config/ncsa_staff.yaml \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n grant-sift rollout restart deploy/grant-sift
```

### 4. Install

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
kubectl -n grant-sift get pods,ingress,pvc
kubectl -n grant-sift logs deploy/grant-sift -f
kubectl -n grant-sift logs -l app.kubernetes.io/name=oauth2-proxy -f
```

Open: **https://grant-sift.software-dev.ncsa.illinois.edu**  
You should bounce through Keycloak (NCSA), then see the dashboard.

Grafana (software-dev): **https://grant-sift-grafana.software-dev.ncsa.illinois.edu** — see [GRAFANA.md](./GRAFANA.md). Seed rollups with `python run.py telemetry` inside the app pod (also runs at the end of the nightly `daily` job).

### 5. Seed data (pick one)

```bash
./scripts/k8s-transfer-db.sh ./grant-sift.db grant-sift grant-sift
```

Or:

```bash
kubectl -n grant-sift exec deploy/grant-sift -- python run.py ingest
kubectl -n grant-sift exec deploy/grant-sift -- python run.py assess --limit 400
kubectl -n grant-sift exec deploy/grant-sift -- python run.py export
```

Run it inside the running pod, not as a separate Job: that pod is the only
writer the database is allowed to have.

### 6. Smoke-check

```bash
kubectl -n grant-sift exec deploy/grant-sift -- python run.py status
curl -sS -o /dev/null -w "%{http_code}\n" https://grant-sift.software-dev.ncsa.illinois.edu/
```

Chat still uses the browser **Personalize** key (not the pipeline Secret).

---

## Auth map

| Setting | Where |
|---|---|
| Issuer `{{keycloak.url}}/realms/{{keycloak.realm}}` | ConfigMap `grant-sift-keycloak` → oauth2-proxy + Grafana OAuth |
| `keycloak.realm` | `values-software-dev.yaml` (default `NCSA`) |
| Client id / secret / cookie (app) | Secret `grant-sift-oauth2` |
| Grafana Keycloak client | Client `grant-sift-grafana`; secret key `client-secret` in `grant-sift-grafana` |
| `GRANT_SIFT_AUTH=proxy` | ConfigMap |
| `GRANT_SIFT_TRUSTED_PROXIES` | `10.42.0.0/16` (k3s pod CIDR) |
| Optional group gate | `GRANT_SIFT_AUTH_REQUIRED_GROUP` |
| Chat API key | Browser Personalize |

To run **without** Keycloak temporarily: set `GRANT_SIFT_AUTH=off`, `oauth2-proxy.enabled=false`, `ingress.enabled=true` in the overlay.

## Upgrade after a new `:main`

```bash
helm upgrade --install grant-sift ./helm/grant-sift \
  -n grant-sift \
  -f helm/grant-sift/values-software-dev.yaml \
  -f helm/grant-sift/values-secrets.yaml
kubectl -n grant-sift rollout restart deploy/grant-sift
```

## Nightly pipeline

`GRANT_SIFT_DAILY_AT: "06:00"` with `GRANT_SIFT_DAILY_TZ: America/Chicago`.

It runs on a background thread inside the serving process, **not** as a separate
pod. SQLite's WAL mode coordinates writers through a shared-memory index that is
only coherent within a single host, so a second pod writing the same file on a
shared volume corrupts the database — which is exactly what happened in
September 2026. One replica, `strategy: Recreate`, and no second writer.

`GRANT_SIFT_DAILY_CATCHUP: "on"` runs the pass at startup when the last one is
over 20h old, so a restart past the scheduled minute does not skip a day.

`run.py daily` ends with a **telemetry** rollup into `telemetry_daily` (same
in-app schedule — not a separate CronJob). Grafana reads those rows via
`/api/stats`. Details: [GRAFANA.md](./GRAFANA.md).

`run.py daily` exports to `web/opportunities.json`, which the entrypoint has symlinked to `/data/opportunities.json`. The running pod serves that file directly (no rebuild, no separate JSON mount). Refresh the browser after a run to see updates (`Cache-Control: no-cache`).

Users subscribe under **Personalize → Email digests**. Addresses land in SQLite `subscribers`; nightly digests email each feed when `GRANT_SIFT_SMTP_HOST` is set.

Campus SMTP (from [Tech Services KB 47888](https://answers.uillinois.edu/illinois/47888)):

| Setting | Value |
|---|---|
| Host | `outbound-relays.techservices.illinois.edu` |
| Port | `25` |
| Auth / TLS | none |
| From | a real deliverable address (e.g. `grant-sift@ncsa.illinois.edu`) |

**Caveat:** that relay requires a campus-recognized source IP. Pods on private `10.x` (k3s) may be refused — if so, switch to [Cloud Email Delivery](https://answers.uillinois.edu/illinois/85362) (SocketLabs) or send from a campus VM with a public/campus IP.

```bash
kubectl -n grant-sift exec deploy/grant-sift -- python run.py digest --feed closing-soon --send
```
