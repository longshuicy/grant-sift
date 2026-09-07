# Helm chart for Grant Sift

Deploys the dashboard (`run.py serve`), a nightly **CronJob** (`run.py daily`), a **PVC** for SQLite, and optionally the official **[oauth2-proxy](https://github.com/oauth2-proxy/manifests)** chart in front of Keycloak.

Not Argo-managed yet — hand-roll with `helm upgrade --install`. Overlay for the NCSA **software-dev** cluster: `values-software-dev.yaml`.

There is no separate SQLite Helm chart — SQLite is a file on the PVC at `/data/grant-sift.db`.

## Layout

| Resource | Purpose |
|---|---|
| Deployment + Service | Web UI, feedback, chat proxy |
| PVC | `grant-sift.db` + `opportunities.json` |
| Secret | `GRANT_SIFT_LLM_API_KEY` (pipeline only) |
| ConfigMap | Non-secret `GRANT_SIFT_*` settings |
| CronJob | Same work as `scripts/daily.sh` |
| oauth2-proxy (subchart) | Keycloak login → identity headers |

## Prerequisites (software-dev)

| Thing | What you have |
|---|---|
| kubectl context | `software-dev` (k3s **1.34**) |
| Helm | **3.9+** (3.9.4 is fine) |
| Ingress | **traefik** + letsencrypt |
| RWX storage | **nfs-taiga** |
| Keycloak | `https://keycloak.software-dev.ncsa.illinois.edu` |
| Image | `ghcr.io/longshuicy/grant-sift` (GitHub Action on merge to `main` / release) |

---

## Step-by-step deploy (hand-roll on software-dev)

### 0. Point at the cluster

```bash
kubectl config use-context software-dev
kubectl get nodes
```

### 1. Publish an image (or wait for CI)

On merge to `main`, `.github/workflows/docker-publish.yml` pushes:

- `ghcr.io/longshuicy/grant-sift:main`
- `ghcr.io/longshuicy/grant-sift:sha-<short>`

On a GitHub **Release** (tag `v1.2.3`):

- `:1.2.3`, `:1.2`, `:1`, `:latest`, plus `:sha-…`

After the first push, open the package on GitHub → **Package settings** → visibility **Public** (simplest), or keep it private and create a pull secret in step 3.

Local one-off (optional):

```bash
docker build -t ghcr.io/longshuicy/grant-sift:main .
echo "$GHCR_PAT" | docker login ghcr.io -u longshuicy --password-stdin
docker push ghcr.io/longshuicy/grant-sift:main
```

### 2. Create a Keycloak client

In Keycloak (`keycloak.software-dev.ncsa.illinois.edu`), pick/create a realm, then a confidential client, e.g. `grant-sift`:

- Valid redirect URIs: `https://grant-sift.software-dev.ncsa.illinois.edu/oauth2/callback`
- Web origins: `https://grant-sift.software-dev.ncsa.illinois.edu`
- Client authentication: **On**
- Note the **client id** and **client secret**

Edit `values-software-dev.yaml` and replace `REALM` in:

```yaml
- --oidc-issuer-url=https://keycloak.software-dev.ncsa.illinois.edu/realms/REALM
```

### 3. Namespace + secrets + roster

```bash
kubectl create namespace grant-sift

# Pipeline LLM key (from your local .env) — chart-managed Secret
cp helm/grant-sift/values-secrets.example.yaml helm/grant-sift/values-secrets.yaml
# put the real GRANT_SIFT_LLM_API_KEY in values-secrets.yaml (gitignored)

# Collaborator roster (gitignored local file — not in the image)
kubectl -n grant-sift create configmap grant-sift-roster \
  --from-file=roster.yaml=config/roster.yaml

# oauth2-proxy Keycloak credentials
COOKIE_SECRET="$(python3 -c 'import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')"
kubectl -n grant-sift create secret generic grant-sift-oauth2 \
  --from-literal=client-id='grant-sift' \
  --from-literal=client-secret='PASTE_KEYCLOAK_CLIENT_SECRET' \
  --from-literal=cookie-secret="$COOKIE_SECRET"
```

Update the roster later without rebuilding:

```bash
kubectl -n grant-sift create configmap grant-sift-roster \
  --from-file=roster.yaml=config/roster.yaml \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n grant-sift rollout restart deploy/grant-sift
```

Private GHCR only:

```bash
kubectl -n grant-sift create secret docker-registry ghcr-pull \
  --docker-server=ghcr.io \
  --docker-username=longshuicy \
  --docker-password=YOUR_GHCR_READ_PAT
# then in values-software-dev.yaml:
# imagePullSecrets: [{ name: ghcr-pull }]
```

### 4. Install the chart

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

Open: `https://grant-sift.software-dev.ncsa.illinois.edu`  
You should bounce through Keycloak, then see the dashboard.

### 5. Load your existing SQLite DB (optional)

```bash
# from the grant-sift repo root, with context still software-dev
./scripts/k8s-transfer-db.sh ./grant-sift.db grant-sift grant-sift
```

Or run one assess pass in-cluster first:

```bash
kubectl -n grant-sift create job --from=cronjob/grant-sift-daily grant-sift-daily-manual
kubectl -n grant-sift logs -f job/grant-sift-daily-manual
```

### 6. Smoke-check

```bash
kubectl -n grant-sift exec deploy/grant-sift -- python run.py status
curl -sS -o /dev/null -w "%{http_code}\n" https://grant-sift.software-dev.ncsa.illinois.edu/
```

Chat still uses the **Personalize** key in the browser (not the pipeline Secret).

---

## Auth reminder

| Setting | Where |
|---|---|
| Keycloak issuer, client id/secret | oauth2-proxy (`grant-sift-oauth2` Secret + issuer URL) |
| `GRANT_SIFT_AUTH=proxy` | ConfigMap via `values-software-dev.yaml` |
| `GRANT_SIFT_TRUSTED_PROXIES` | `10.42.0.0/16` (k3s pod CIDR on software-dev) |
| Chat `sk_` key | Browser Personalize panel |

Local / no Keycloak:

```yaml
config:
  GRANT_SIFT_AUTH: "off"
oauth2-proxy:
  enabled: false
ingress:
  enabled: true
```

## Upgrade later

```bash
# after CI publishes a new :main (or pin a release tag in values-software-dev.yaml)
helm upgrade --install grant-sift ./helm/grant-sift \
  -n grant-sift \
  -f helm/grant-sift/values-software-dev.yaml \
  -f helm/grant-sift/values-secrets.yaml

kubectl -n grant-sift rollout restart deploy/grant-sift
```

## CronJob

Default in the software-dev overlay: `0 6 * * *` America/Chicago.

```bash
kubectl -n grant-sift create job --from=cronjob/grant-sift-daily grant-sift-daily-manual
```
