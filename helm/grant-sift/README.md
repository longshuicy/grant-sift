# Helm chart for Grant Sift

Deploys the dashboard (`run.py serve`), a nightly **CronJob** (`run.py daily`), a **PVC** for SQLite, and **[oauth2-proxy](https://github.com/oauth2-proxy/manifests)** in front of Keycloak.

Not Argo-managed yet — hand-roll with `helm upgrade --install`. Cluster overlay: `values-software-dev.yaml`.

SQLite is a file on the PVC (`/data/grant-sift.db`); there is no separate SQLite chart.

## Layout

| Resource | Purpose |
|---|---|
| Deployment + Service | Web UI, feedback, chat proxy (ClusterIP only) |
| oauth2-proxy + Ingress | Traefik → Keycloak login → app |
| PVC (`nfs-taiga`) | Shared `/data`: `grant-sift.db` + `opportunities.json` (Deployment **and** CronJob) |
| Secret | `GRANT_SIFT_LLM_API_KEY` (pipeline) + `grant-sift-oauth2` (OIDC client) |
| ConfigMap | Non-secret env + mounted `roster.yaml` |
| CronJob | Same work as `scripts/daily.sh` |

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

### 5. Seed data (pick one)

```bash
./scripts/k8s-transfer-db.sh ./grant-sift.db grant-sift grant-sift
```

Or:

```bash
kubectl -n grant-sift create job --from=cronjob/grant-sift-daily grant-sift-daily-manual
kubectl -n grant-sift logs -f job/grant-sift-daily-manual
```

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
| Issuer `{{keycloak.url}}/realms/{{keycloak.realm}}` | ConfigMap `grant-sift-keycloak` → oauth2-proxy env |
| `keycloak.realm` | `values-software-dev.yaml` (default `NCSA`) |
| Client id / secret / cookie | Secret `grant-sift-oauth2` |
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

## CronJob

`0 6 * * *` America/Chicago.

The CronJob mounts the **same PVC** as the dashboard at `/data`. `run.py daily` exports to `web/opportunities.json`, which the entrypoint has symlinked to `/data/opportunities.json`. The running pod serves that file directly (no rebuild, no separate JSON mount). Refresh the browser after a run to see updates (`Cache-Control: no-cache`).

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
kubectl -n grant-sift create job --from=cronjob/grant-sift-daily grant-sift-daily-manual
```
