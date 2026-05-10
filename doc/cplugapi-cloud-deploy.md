# cplugapi cloud deployment runbook

Operational reference for deploying the `cplug_webui` fork to a single
cloud replica behind an ingress. Sibling document to `doc/cplugapi.md`
(API contract) and `doc/cplugapi-threat-model.md` (W19, threat surface).

This runbook is the OPERATOR view: env vars, manifests, probes, log
ingestion, drain semantics, and the most common bring-up failures. It
does NOT restate the API surface; consult `doc/cplugapi.md` for that.

## When to use cloud profile

`CPLUG_DEPLOYMENT_PROFILE=cloud` flips a coordinated bundle of defaults
suited to single-replica deployment behind an ingress (TLS at the
ingress, Basic auth at the ingress + at the fork, fork bound to
`0.0.0.0`). The profile selects:

| Knob                      | `desktop` (default)              | `cloud`                              |
|---------------------------|----------------------------------|--------------------------------------|
| `ALLOWED_HOSTS` default   | `127.0.0.1, localhost, [::1]`    | `*` (any non-empty `Host`)           |
| `ALLOWED_ORIGINS` default | loopback regex                   | `*` (any non-empty `Origin`)         |
| `auto_preempt` default    | `always` (sketch workflow)       | `off` (don't cancel paid gens)       |
| Rate-limit defaults       | every class off                  | mutating=30/min, read=600/min, auth_failed=10/min |
| `SHUTDOWN_REJECT_NEW`     | `0` (let the in-flight gen win)  | `1` (reject new POSTs while draining)|

Use cloud profile when ALL of the following hold:

- The fork is reachable via TLS terminated at an ingress (not direct).
- Browser-side callers may exist (cloud profile assumes the ingress and
  `Sec-Fetch-Site` together police cross-origin abuse — the fork's
  `Host` allow-list is delegated to the ingress's vhost routing).
- Clients are remote, so per-IP rate-limit keying actually
  discriminates; loopback-only deployments key on `Authorization` hash
  instead and would over-throttle distinct clients sharing a credential.
- You accept the single-replica constraint below.

### Multi-replica is NOT supported

Out of scope per `plan/cplugapi-world-class.md` §1 non-goals.
Specifically:

- **Session state is per-process.** The idempotency cache, queue
  registry, and cancelled-task set live in process memory. A second
  replica behind the same ingress would silently desynchronise on
  retry / cancel paths.
- **Rate-limit state is per-process.** Two replicas mean a caller with
  one bucket on each gets effectively 2× the configured cap.
- **No sticky-session contract.** The Rust client pins to one base URL
  but assumes process-affinity for in-progress task IDs; round-robin
  ingress to two replicas breaks `/session/cancel/{id_task}`.

If you need HA, run a single replica with N+1 redundancy via the
orchestrator (rolling restart pattern below) — not active/active.

`/identify.capabilities[]` and `/health.capabilities[]` advertise
`deployment-profile-cloud` only when the profile is active. Clients can
detect the posture before authenticating.

## Required environment variables

Cloud profile is "fail closed" — these MUST be present for a healthy
boot. The fork raises at startup if any are missing in a context that
needs them.

| Variable                       | Required when                                                               | Notes |
|--------------------------------|-----------------------------------------------------------------------------|-------|
| `CPLUG_DEPLOYMENT_PROFILE=cloud` | Always, on cloud replicas                                                  | Without this, the fork boots in `desktop` mode and rejects every non-loopback `Host`. |
| `--api-auth user:pass`         | Always (CLI flag, not env)                                                  | Pass on the launcher command line. The cplugapi surface inherits this auth. There is no second auth layer; do not invent one. |
| `CPLUG_TRUSTED_PROXIES=<CIDRs>`  | Whenever ANY rate-limit class is enabled (cloud default enables all three) | Comma-separated CIDRs. Without this, `validate_startup()` raises and the process exits. See "Identifying the ingress CIDR" below. |

### Identifying the ingress CIDR

Rate limiting in cloud profile keys on the real client IP, recovered by
walking `X-Forwarded-For` from right to left, skipping addresses that
fall inside `CPLUG_TRUSTED_PROXIES`. If the immediate TCP peer (the
ingress) is not itself in the trusted list, the XFF chain is ignored
entirely and the peer IP becomes the key — meaning every request from
your ingress shares one bucket and the rate limiter is effectively
bypassable by anyone behind that ingress.

Pick the right CIDR for your environment:

| Environment                 | Typical trusted-proxy source                                |
|-----------------------------|-------------------------------------------------------------|
| k8s with NodePort / cluster IP | The pod CIDR for the ingress controller (e.g. `10.244.0.0/16` for kubeadm defaults). Run `kubectl get pods -n ingress-nginx -o wide` and pick the `IP` column's network. |
| k8s with hostNetwork ingress | The node CIDR (`10.0.0.0/16` typical). |
| AWS ALB / NLB → ECS         | The VPC CIDR (`10.0.0.0/16` typical) — the ALB lives in the VPC. |
| Cloudflare proxy → origin   | The published Cloudflare ranges (`https://www.cloudflare.com/ips-v4/` and `/ips-v6/`). Refresh quarterly; Cloudflare publishes a stable list. |
| Fly.io behind their proxy   | `fdaa::/16` (Fly's internal IPv6) plus `0.0.0.0/0` IF your fly.toml routes through the public proxy (Fly does the auth-trust at their edge). |
| Cloud Run                   | Google's frontend (`130.211.0.0/22`, `35.191.0.0/16`) plus the cluster-internal range. |
| Plain Docker on a VM behind nginx | `127.0.0.1/32` (when nginx is on the same host) or the bridge network range (`172.17.0.0/16` for the default Docker bridge). |

**Sanity check before going live**: send `curl -H 'Host: api.example.com'
-H 'X-Forwarded-For: 1.2.3.4, 10.0.0.5'` from your ingress at a
diagnostic endpoint. If the rate-limit `X-RateLimit-*` headers come back,
the bucket is keyed on `1.2.3.4`. If they're absent or the bucket clearly
shares with other testers, your `CPLUG_TRUSTED_PROXIES` is wrong.

## Recommended environment variables

These are not strictly required but improve the operability of a cloud
deployment.

| Variable                          | Recommended value                       | Why |
|-----------------------------------|-----------------------------------------|-----|
| `CPLUG_LOG_FORMAT=json`           | `json`                                  | Cplugapi-owned loggers (`cplugapi.access`, `cplugapi.sdapi`, `cplugapi.gen_timing`, `cplugapi.upscale`, `cplugapi.preempt`, `cplugapi.ws_auth`) emit one JSON object per line instead of key=value text. Loki / Filebeat / CloudWatch can index fields without regex parsing. |
| `CPLUG_ALLOWED_HOSTS`             | `api.example.com,api-internal.example.com` | Narrower than the cloud-default wildcard. Defence in depth against a misconfigured ingress that forwards arbitrary `Host`. |
| `CPLUG_ALLOWED_ORIGINS`           | `https://app.example.com`               | Narrower than the cloud-default wildcard. Cuts the cross-origin attack surface to just your published web frontend. The `Sec-Fetch-Site: cross-site` rejection still fires regardless. |
| `CPLUG_SHUTDOWN_GRACE_S=25`       | `25`                                    | Match to your orchestrator's `terminationGracePeriodSeconds` minus 5s. The 5s pad lets the SIGTERM bridge schedule, the drain flag flip, and the orchestrator pull the pod from rotation before SIGKILL. |
| `CPLUG_METRICS_PUBLIC=1`          | `1` if scraping with no creds           | Mounts `/metrics` on the public router. Use when a sidecar Prometheus or external scraper cannot inject Basic auth. Leave unset to keep `/metrics` auth-gated. |
| `CPLUG_FORK_COMMIT`               | `${CI_COMMIT_SHA}`                      | Surfaces in `/identify.fork_commit` and `/version.fork_commit`. CI pipelines should set this so deploys are correlatable to git. |
| `CPLUG_UPSTREAM_COMMIT`           | upstream `forge-neo` SHA at fork time   | Same as above, for the merge-base SHA. Lets clients tell which upstream features are present without scanning the changelog. |
| `CPLUG_FORK_BUILD_DATE`           | unset; falls back to process start ISO  | Override only if you build images deterministically and want the build date instead of the process start date in `/version`. |
| `CPLUG_ACCESS_LOG=1`              | `1`                                     | Per-request log line for `/cplugapi/v1/*`. Required for the metrics handler to count requests (the metrics module is a `logging.Handler` attached to `cplugapi.access`). |
| `CPLUG_SDAPI_OBSERVER=1`          | `1` if you want per-`/sdapi/v1/*` lines | Useful for diagnosing client behaviour; floods at 4 Hz client polling cadence. Toggle off after triage. |
| `CPLUG_GEN_TIMING=1`              | `1` for first-day operability           | One line per `process_images_inner` with `total_ms`, `vae_decode_ms`, `peak_vram_mb`. The single most useful debugging lever when gens are slow. |

`CPLUG_ACCESS_LOG=1` is technically optional but the
`cplugapi_requests_total` and `cplugapi_request_duration_seconds`
metrics derive from it via `metrics._MetricsLogHandler`. With
`CPLUG_ACCESS_LOG=0`, `/metrics` exposes only the gauge and the
idempotency-replay counter.

## Ingress configuration

The fork binds plain HTTP. TLS is terminated at the ingress. Three
invariants the ingress MUST satisfy:

1. **Forward `X-Forwarded-For`.** Every common ingress does this by
   default (nginx-ingress, ALB, Cloudflare, Fly, Cloud Run). Verify with
   a debug log line in the cplugapi access log; the request's resolved
   client IP should be a remote address, not the ingress.
2. **Probe paths are unauthenticated.** `/cplugapi/v1/livez` and
   `/cplugapi/v1/readyz` are mounted on the public router (W1) and
   never require Basic auth, even when `--api-auth` is set. Configure
   the orchestrator's health probe at these paths — never at
   `/cplugapi/v1/health`, which IS auth-gated.
3. **Pass through Basic auth headers.** The ingress MUST forward
   `Authorization: Basic ...` to the fork. Some ingresses (notably
   AWS API Gateway HTTP API) strip auth headers by default; configure
   them through.

### nginx-ingress fragment

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: cplugapi
  annotations:
    nginx.ingress.kubernetes.io/proxy-body-size: "32m"   # match CPLUG_MAX_BODY_BYTES
    nginx.ingress.kubernetes.io/proxy-read-timeout: "180" # gens take seconds; default 60s is too short
    nginx.ingress.kubernetes.io/proxy-send-timeout: "180"
    nginx.ingress.kubernetes.io/configuration-snippet: |
      # Pass the real client IP. nginx-ingress does this by default,
      # snippet shown for explicitness.
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
spec:
  tls:
    - hosts: [api.example.com]
      secretName: api-example-tls
  rules:
    - host: api.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: cplugapi
                port:
                  number: 7860
```

### AWS ALB target group

```yaml
HealthCheckPath: /cplugapi/v1/readyz
HealthCheckPort: '7860'
HealthCheckProtocol: HTTP
HealthCheckIntervalSeconds: 15
HealthCheckTimeoutSeconds: 5
HealthyThresholdCount: 2
UnhealthyThresholdCount: 3
Matcher:
  HttpCode: '200'        # 503 during drain → marks unhealthy → ALB stops routing
TargetType: ip
Attributes:
  - Key: deregistration_delay.timeout_seconds
    Value: '30'          # >= CPLUG_SHUTDOWN_GRACE_S
  - Key: stickiness.enabled
    Value: 'false'       # single replica; no sticky needed
```

Ensure the ALB security group allows the source-IP-preserving
listener (or use NLB if you need TCP-level src IP). With a
non-preserving ALB, set `CPLUG_TRUSTED_PROXIES` to the VPC CIDR; the
ALB rewrites the peer IP to its own ENI but still forwards the real
client in `X-Forwarded-For`.

### Cloud Run

```yaml
metadata:
  annotations:
    run.googleapis.com/execution-environment: gen2  # gVisor-incompatible CUDA wants gen2
spec:
  template:
    spec:
      containerConcurrency: 1   # single replica; never co-schedule gens
      timeoutSeconds: 600        # gens > 60s default
      containers:
        - image: gcr.io/PROJECT/cplug_webui:TAG
          ports:
            - containerPort: 7860
          env:
            - name: CPLUG_DEPLOYMENT_PROFILE
              value: cloud
            - name: CPLUG_TRUSTED_PROXIES
              # Google Frontend ranges + Cloud Run's internal CIDR.
              value: "130.211.0.0/22,35.191.0.0/16,169.254.8.0/22"
          startupProbe:
            httpGet: { path: /cplugapi/v1/livez, port: 7860 }
            failureThreshold: 30
            periodSeconds: 10
          livenessProbe:
            httpGet: { path: /cplugapi/v1/livez, port: 7860 }
            periodSeconds: 30
```

`containerConcurrency: 1` is mandatory — Cloud Run normally
fan-outs requests across replicas, but the fork is single-replica
and would corrupt session state if Cloud Run ran two parallel.

### Fly.io

```toml
# fly.toml
app = "cplugapi"
primary_region = "iad"

[[services]]
  internal_port = 7860
  protocol = "tcp"

  [[services.ports]]
    port = 443
    handlers = ["tls", "http"]

  [services.concurrency]
    type = "connections"
    hard_limit = 25
    soft_limit = 20

  [[services.tcp_checks]]
    interval = "30s"
    timeout = "5s"
    grace_period = "60s"
    restart_limit = 0

  [[services.http_checks]]
    interval = "15s"
    timeout = "5s"
    grace_period = "60s"
    method = "get"
    path = "/cplugapi/v1/readyz"
    protocol = "http"
    tls_skip_verify = false

[deploy]
  strategy = "rolling"          # 1-replica rolling restart
  max_unavailable = 0
  release_command_timeout = "60s"

[env]
  CPLUG_DEPLOYMENT_PROFILE = "cloud"
  CPLUG_TRUSTED_PROXIES = "fdaa::/16,213.188.192.0/22"  # Fly internal + edge ranges
  CPLUG_LOG_FORMAT = "json"
  CPLUG_SHUTDOWN_GRACE_S = "25"
```

Auth is loaded from a Fly secret: `fly secrets set CPLUG_API_AUTH=user:pass`
and pass through to the launcher in your start command.

### Plain Docker behind nginx

```nginx
server {
    listen 443 ssl http2;
    server_name api.example.com;

    ssl_certificate     /etc/letsencrypt/live/api.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.example.com/privkey.pem;

    client_max_body_size 32m;        # match CPLUG_MAX_BODY_BYTES

    location / {
        proxy_pass         http://127.0.0.1:7860;
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;

        # gens take time
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }

    # Health probes — keep them off the auth-required upstream.
    location = /healthz {
        proxy_pass http://127.0.0.1:7860/cplugapi/v1/livez;
        access_log off;
    }
}
```

With `CPLUG_TRUSTED_PROXIES=127.0.0.1/32` the fork accepts XFF from
the local nginx and keys rate limits on the real remote IP nginx
forwards.

## Kubernetes manifest

A complete-enough Deployment + Service example. Adjust GPU node
selector / tolerations to your cluster.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: cplugapi-env
type: Opaque
stringData:
  # Basic auth string — passed to the launcher as --api-auth $(value)
  CPLUG_API_AUTH: "ops:<long-random-here>"
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: cplugapi-config
data:
  CPLUG_DEPLOYMENT_PROFILE: "cloud"
  CPLUG_LOG_FORMAT: "json"
  CPLUG_ALLOWED_HOSTS: "api.example.com,api-internal.example.com"
  CPLUG_ALLOWED_ORIGINS: "https://app.example.com"
  # Match the ingress's pod CIDR + service CIDR.
  CPLUG_TRUSTED_PROXIES: "10.244.0.0/16,10.96.0.0/12"
  CPLUG_SHUTDOWN_GRACE_S: "25"
  CPLUG_ACCESS_LOG: "1"
  CPLUG_SDAPI_OBSERVER: "1"
  CPLUG_GEN_TIMING: "1"
  CPLUG_METRICS_PUBLIC: "0"  # private; Prometheus scrapes with creds
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cplugapi
  labels:
    app: cplugapi
spec:
  replicas: 1                   # single-replica only; see "rolling restart pattern"
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 0
      maxSurge: 1               # bring up the new pod before terminating the old
  selector:
    matchLabels: { app: cplugapi }
  template:
    metadata:
      labels: { app: cplugapi }
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "7860"
        prometheus.io/path: "/cplugapi/v1/metrics"
    spec:
      terminationGracePeriodSeconds: 30   # >= CPLUG_SHUTDOWN_GRACE_S + 5
      nodeSelector:
        cloud.google.com/gke-accelerator: nvidia-l4
      tolerations:
        - key: nvidia.com/gpu
          operator: Exists
          effect: NoSchedule
      # If checkpoints come from object storage, an init container
      # warms the model directory before the main container serves.
      initContainers:
        - name: model-fetch
          image: gcr.io/PROJECT/model-sync:v1
          command:
            - /bin/sh
            - -c
            - "rclone copy s3:checkpoints/sdxl /models --transfers 4"
          volumeMounts:
            - { name: models, mountPath: /models }
          resources:
            requests: { cpu: 100m, memory: 256Mi }
            limits:   { cpu: 1,    memory: 1Gi }
      containers:
        - name: cplugapi
          image: ghcr.io/your-org/cplug_webui:0.1.0
          imagePullPolicy: IfNotPresent
          args:
            - --api
            - --api-auth=$(CPLUG_API_AUTH)
            - --listen
            - --port=7860
          ports:
            - { name: http, containerPort: 7860 }
          envFrom:
            - secretRef:    { name: cplugapi-env }
            - configMapRef: { name: cplugapi-config }
          # Probes hit the unauth public endpoints (W1).
          startupProbe:
            httpGet:
              path: /cplugapi/v1/livez
              port: http
            failureThreshold: 60     # checkpoint load can take >5 min
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /cplugapi/v1/livez
              port: http
            periodSeconds: 30
            timeoutSeconds: 5
            failureThreshold: 3
          readinessProbe:
            httpGet:
              path: /cplugapi/v1/readyz
              port: http
            periodSeconds: 10
            timeoutSeconds: 3
            failureThreshold: 1     # 503 immediately pulls from rotation
          resources:
            requests:
              cpu: "2"
              memory: 16Gi
              nvidia.com/gpu: "1"
            limits:
              cpu: "8"
              memory: 32Gi
              nvidia.com/gpu: "1"
          volumeMounts:
            - { name: models,  mountPath: /workspace/models }
            - { name: outputs, mountPath: /workspace/outputs }
      volumes:
        - name: models
          persistentVolumeClaim: { claimName: cplugapi-models }
        - name: outputs
          emptyDir:
            sizeLimit: 10Gi
---
apiVersion: v1
kind: Service
metadata:
  name: cplugapi
  labels: { app: cplugapi }
spec:
  type: ClusterIP
  ports:
    - { name: http, port: 7860, targetPort: http }
  selector:
    app: cplugapi
```

Key points:

- `replicas: 1`. Multi-replica is unsupported; `maxSurge: 1 +
  maxUnavailable: 0` is the rolling-restart trick that keeps one pod
  serving while the new one warms.
- `terminationGracePeriodSeconds: 30 >= CPLUG_SHUTDOWN_GRACE_S + 5`.
  When the orchestrator kills before the fork finishes draining, the
  graceful-shutdown sequence is wasted. Always pad the orchestrator
  side.
- The init container is for clusters where checkpoints aren't baked
  into the image. Skip it if the image has the model directory already
  populated.
- `nvidia.com/gpu: "1"` requests one GPU; `containerConcurrency` (Cloud
  Run) or `replicas: 1` (k8s) is what enforces single-process serving.
- The probes deliberately point at unauthenticated endpoints; if the
  probes start returning 401, somebody has changed the path to
  `/health` or `/version` (both auth-gated).

## Prometheus scrape config

Cplugapi exposes a vendored Prometheus exposition endpoint at
`/cplugapi/v1/metrics`. Under the cloud profile defaults it is
auth-gated (mounted on the private router); flip
`CPLUG_METRICS_PUBLIC=1` to mount it on the public router.

### Auth-gated scrape (default)

```yaml
# prometheus.yml
scrape_configs:
  - job_name: cplugapi
    metrics_path: /cplugapi/v1/metrics
    scheme: https
    scrape_interval: 15s
    scrape_timeout: 10s
    basic_auth:
      username: ops
      password_file: /etc/prometheus/cplugapi-pw
    static_configs:
      - targets: ['api.example.com:443']
        labels:
          service: cplugapi
          env: prod
```

### Public scrape (`CPLUG_METRICS_PUBLIC=1`)

```yaml
scrape_configs:
  - job_name: cplugapi
    metrics_path: /cplugapi/v1/metrics
    scrape_interval: 15s
    static_configs:
      - targets: ['cplugapi.cplugapi-prod.svc.cluster.local:7860']
```

15-second cadence is the right floor for the fork — gen latency is in
the seconds-to-minutes range, the histogram buckets bottom out at 5ms
(below which there is no useful resolution), and the `requests_total`
counter doesn't move fast enough to need 5s scrapes.

### Metrics actually exposed

The exposition body contains exactly four metric families:

| Metric                                      | Type      | Labels              | What it measures |
|---------------------------------------------|-----------|---------------------|------------------|
| `cplugapi_requests_total`                   | counter   | method, path, status | One increment per `/cplugapi/v1/*` request handled. `path` is normalised to template form (`/session/cancel/{id_task}`). |
| `cplugapi_request_duration_seconds`         | histogram | method, path        | Wall-clock spent server-side. Buckets: 5ms, 10ms, 25ms, 50ms, 100ms, 250ms, 500ms, 1s, 2.5s, 5s, 10s, +Inf. |
| `cplugapi_idempotency_replays_total`        | counter   | (none)              | One increment per response carrying `Idempotency-Replayed: true`. |
| `cplugapi_active_task_id_present`           | gauge     | (none)              | 1 when a gen task is currently running, 0 otherwise. Sampled at scrape time. |

There is **no** `cplugapi_rate_limit_hits_total` metric today — rate
limit fires surface only via 429 responses (visible as `status="429"`
in `cplugapi_requests_total`) and via the `Retry-After` /
`X-RateLimit-*` headers on those responses. The "rate-limit fires"
alert below uses the 429-status counter.

### Sample alert rules

```yaml
# cplugapi-alerts.yml
groups:
  - name: cplugapi.rules
    interval: 30s
    rules:
      # Server error rate >1% over 5m. /cplugapi/v1/* should be quiet
      # in steady state — bursts of 5xx mean a real bug.
      - alert: CplugapiHighErrorRate
        expr: |
          sum(rate(cplugapi_requests_total{status=~"5.."}[5m]))
            /
          sum(rate(cplugapi_requests_total[5m]))
            > 0.01
        for: 5m
        labels: { severity: page }
        annotations:
          summary: "cplugapi 5xx rate >1% over 5m"
          description: "{{ $value | humanizePercentage }} of /cplugapi/v1/* responses are 5xx"

      # Rate limit firing — derive from 429 status counter.
      - alert: CplugapiRateLimitFiring
        expr: |
          sum(rate(cplugapi_requests_total{status="429"}[5m])) > 0
        for: 5m
        labels: { severity: warn }
        annotations:
          summary: "cplugapi rate-limited a client"
          description: "Either a legitimate client is over its budget (raise CPLUG_RATE_LIMIT_*) or CPLUG_TRUSTED_PROXIES is wrong (every client shares one bucket)"

      # Active task stuck — gen running for >10m.
      - alert: CplugapiActiveTaskStuck
        expr: |
          cplugapi_active_task_id_present == 1
        for: 10m
        labels: { severity: warn }
        annotations:
          summary: "cplugapi has a task running longer than 10m"
          description: "Either a real long gen, or a hung gen that needs /session/preempt. Check /cplugapi/v1/queue."

      # P99 latency on read endpoints. /health and /queue should be sub-50ms.
      - alert: CplugapiReadLatencyHigh
        expr: |
          histogram_quantile(0.99,
            sum by (le, path) (
              rate(cplugapi_request_duration_seconds_bucket{path=~"/cplugapi/v1/(health|queue|identify)"}[5m])
            )
          ) > 0.5
        for: 10m
        labels: { severity: warn }
        annotations:
          summary: "cplugapi read endpoint p99 > 500ms"
          description: "{{ $labels.path }} p99 = {{ $value }}s; usually means GIL contention from a hot gen"

      # Idempotency replays — high rate suggests retry storm from client.
      - alert: CplugapiIdempotencyReplayStorm
        expr: |
          rate(cplugapi_idempotency_replays_total[5m]) > 1
        for: 10m
        labels: { severity: warn }
        annotations:
          summary: "cplugapi serving >1 replay/sec sustained"
          description: "Client is retrying with same Idempotency-Key — likely a network or timeout issue on the client side"

      # No request traffic — pod is up but nothing is hitting it.
      - alert: CplugapiNoTraffic
        expr: |
          sum(rate(cplugapi_requests_total[15m])) == 0
        for: 30m
        labels: { severity: info }
        annotations:
          summary: "cplugapi has had no requests in 30m"
          description: "Pod is healthy per /readyz but no client traffic — DNS issue, ingress misconfigured, or the client is offline"
```

Tune the thresholds to your traffic shape. The `CplugapiNoTraffic`
alert is information-only because a single-replica fork is often used
for lightly trafficked workloads and "no traffic" can be the steady
state.

## Loki / ELK config for `CPLUG_LOG_FORMAT=json`

With JSON logging on, every line emitted by the cplugapi-owned loggers
is one JSON object per stdout line. The Forge / WebUI's own loggers
keep their existing format (invariant 1 — sdapi byte-identity demands
this).

### Field reference

The cplugapi JSON formatter writes a fixed set of top-level keys plus
all `extra={...}` keys the call site attached:

| Key            | Origin                                | Meaning |
|----------------|---------------------------------------|---------|
| `ts`           | formatter                             | ISO-8601 UTC, ms precision |
| `level`        | formatter                             | `INFO`, `WARNING`, `ERROR` |
| `logger`       | formatter                             | Logger name (`cplugapi.access`, `cplugapi.sdapi`, etc.) |
| `msg`          | formatter                             | Rendered message string |
| `method`       | `cplugapi.access`, `cplugapi.sdapi`   | HTTP verb |
| `path`         | `cplugapi.access`, `cplugapi.sdapi`   | Request path (concrete, not template-normalised) |
| `status`       | `cplugapi.access`, `cplugapi.sdapi`   | Response status code |
| `dur_ms`       | `cplugapi.access`, `cplugapi.sdapi`   | Wall-clock spent server-side |
| `in_bytes`     | `cplugapi.access`, `cplugapi.sdapi`   | Request `Content-Length`, or `-1` |
| `out_bytes`    | `cplugapi.access`                     | Response `Content-Length`, or `-1` |
| `request_id`   | `cplugapi.access`                     | `X-Request-Id` value |
| `replayed`     | `cplugapi.access`                     | `1` when an idempotency replay |
| `error`        | `cplugapi.access`                     | Exception class name when handler raised |
| `traceparent`  | tracing module                        | W3C trace context, when forwarded by the client |
| `trace_id`     | tracing module                        | First 32 hex chars of `traceparent`, for log correlation |
| `total_ms`     | `cplugapi.gen_timing`                 | `process_images_inner` total wall-clock |
| `vae_decode_ms`| `cplugapi.gen_timing`                 | VAE decode time |
| `peak_vram_mb` | `cplugapi.gen_timing`                 | Peak CUDA memory |
| `type`         | `cplugapi.upscale`                    | `extras` or `img2img-refine` |

### Promtail (Loki)

```yaml
scrape_configs:
  - job_name: cplugapi
    pipeline_stages:
      - json:
          expressions:
            level: level
            logger: logger
            method: method
            path: path
            status: status
            dur_ms: dur_ms
            request_id: request_id
            error: error
      - labels:
          level:
          logger:
          method:
          path:
          status:
      - timestamp:
          source: ts
          format: RFC3339Nano
    static_configs:
      - targets: [localhost]
        labels:
          job: cplugapi
          __path__: /var/log/pods/cplugapi-*/cplugapi/*.log
```

Cardinality watch: `path` as a label can blow up if the access log
emits concrete paths (it does — only the metrics module
template-normalises). For Loki you want either a derived field or
`drop` for high-cardinality paths; the example above promotes them
to labels for ease of demo, not production. In production, label
on `level`, `logger`, `status`, and use line-search for `path`.

### Filebeat → Elasticsearch

```yaml
filebeat.inputs:
  - type: container
    paths:
      - /var/log/containers/cplugapi-*.log
    json:
      keys_under_root: true
      add_error_key: true
      message_key: msg
    processors:
      - drop_event:
          when:
            not:
              equals:
                logger: cplugapi.access  # keep only access logs in this stream
output.elasticsearch:
  hosts: ["https://es.example.com:9200"]
  index: "cplugapi-access-%{+yyyy.MM.dd}"
  pipeline: "cplugapi-pipeline"
```

The Forge / WebUI logs (Gradio, the upstream FastAPI app, etc.) are
NOT JSON — they keep their text format. Route them to a different
index so the schema mismatch doesn't break Elasticsearch's mapping.

### Recommended dashboards

Three panels are usually sufficient for first-week operation:

1. **Per-route p50/p99 latency** (from `cplugapi.access`). Group by
   `path` and `method`, plot percentiles of `dur_ms`. The fast endpoints
   (`/health`, `/queue`, `/identify`) should sit at single-digit ms;
   `/forge/preset` and `/session/*` likewise. Anything spiking is
   GIL contention from a hot gen.
2. **Auth-failure rate** (from `cplugapi.access`, filtered to
   `status=401` or `status=403` with `code` in the body's
   problem+json). A spike means either a credential rotation that
   missed a client, or a brute-force attempt — cross-check against
   `cplugapi_requests_total{status="429"}` to see if the auth_failed
   rate-limiter has kicked in.
3. **Gen pipeline timing** (from `cplugapi.gen_timing`). Plot
   `total_ms` and `vae_decode_ms` per gen. The difference is "everything
   else" (conditioning, sampling, save). Track `peak_vram_mb` against
   your card's total — sustained values within 1 GiB of total mean the
   driver is about to spill to shared memory and slow gens 10-20×.

## Rolling restart pattern

The fork is single-replica. A "rolling restart" therefore means:

1. Orchestrator brings up replica B with a fresh image.
2. Replica B passes its readiness probe (boots, loads checkpoint,
   `/readyz` returns 200). This typically takes 60-180 seconds for
   a cold checkpoint load.
3. Orchestrator updates the service to route to B.
4. Orchestrator sends SIGTERM to replica A.
5. Replica A's signal handler runs the graceful shutdown sequence:
   - `livez_readyz.set_draining(True)` — `/readyz` flips to 503 with
     `{"draining": true}` in the public-body checks. Orchestrator
     observes this on its next probe and stops sending traffic
     (~10s lag with `periodSeconds: 10` + `failureThreshold: 1`).
   - `RejectDuringDrainMiddleware` (cloud profile default) returns
     503 + `Retry-After: 5` for any new POST/PUT/PATCH/DELETE to
     `/cplugapi/v1/*` or to `/sdapi/v1/{txt2img,img2img}`. GETs pass
     through.
   - In-flight gens have up to `CPLUG_SHUTDOWN_GRACE_S` (default 30s,
     recommended 25s) to finish.
   - At grace expiry, `shared.state.interrupt()` aborts whatever's
     still running.
6. Orchestrator sends SIGKILL after `terminationGracePeriodSeconds`
   (set this to `CPLUG_SHUTDOWN_GRACE_S + 5` so the fork's grace
   window completes inside the orchestrator's).

### Critical timing constraints

```text
                              SIGTERM            SIGKILL
                                 |                  |
replica A: serving ----[draining]====[grace=25s]==[interrupt]X
                                 |                  |
                                 +-- /readyz=503    +-- terminationGracePeriodSeconds=30
replica B: starting ====[ready]------serving---------------serving
                              |
                              +-- orchestrator switches traffic when readyz=200
```

The 5-second pad between `CPLUG_SHUTDOWN_GRACE_S` and
`terminationGracePeriodSeconds` is the operational margin. Without it,
the orchestrator SIGKILLs while `interrupt()` is still propagating
through Forge's sample loop; the half-rendered preview disappears
mid-frame. With it, the gen aborts cleanly, the pod exits cleanly,
the orchestrator records a graceful termination.

### `preStop` hook

Not needed. The fork's SIGTERM handler does the drain work. Adding a
`preStop` `sleep` would only delay SIGTERM delivery without changing
the drain semantics. The k8s docs sometimes recommend a `sleep` to
absorb endpoint-controller propagation lag — that's already handled by
the readiness probe flipping at SIGTERM-time.

If your orchestrator does NOT deliver SIGTERM at all (some custom
schedulers): invoke `graceful_shutdown()` from a sidecar via a
`preStop` HTTP call, but this isn't a path the fork tests.

## Troubleshooting

| Symptom                                           | Likely cause                                                                                              | Fix |
|---------------------------------------------------|-----------------------------------------------------------------------------------------------------------|-----|
| 403 `host_not_allowed` on every request           | `CPLUG_DEPLOYMENT_PROFILE=cloud` not set, OR `CPLUG_ALLOWED_HOSTS` doesn't contain the real `Host` header  | Set `CPLUG_DEPLOYMENT_PROFILE=cloud` (gives wildcard) or set `CPLUG_ALLOWED_HOSTS=api.example.com,...` explicitly. |
| Process crashes at boot with `CPLUG_TRUSTED_PROXIES is unset` | Cloud profile + at least one rate-limit class active, but no trusted-proxy CIDRs configured  | Set `CPLUG_TRUSTED_PROXIES=<ingress CIDR>`. To disable rate limiting entirely, set `CPLUG_RATE_LIMIT_MUTATING=0`, `CPLUG_RATE_LIMIT_READ=0`, `CPLUG_RATE_LIMIT_AUTH_FAILED=0`. |
| 429 `rate_limited` on a legitimate client         | Either the client truly exceeds 30 mutating / 600 read per minute, OR every client shares one bucket because `CPLUG_TRUSTED_PROXIES` doesn't match the actual ingress | Verify `X-RateLimit-Remaining` decrements per-client (different remote IPs should have separate counters). If they share, fix `CPLUG_TRUSTED_PROXIES`. If real, raise `CPLUG_RATE_LIMIT_READ` or `CPLUG_RATE_LIMIT_MUTATING`. |
| `/readyz` returns 503 with `model_loaded: false`  | Initial checkpoint not loaded — model directory mount empty, or checkpoint name in `sd_model_checkpoint` doesn't match a file on disk | Verify the `models` PVC is populated (`kubectl exec ... -- ls /workspace/models/Stable-diffusion`). Check the launcher's `--ckpt-dir` matches. |
| `/readyz` returns 503 with `has_error: true`      | A module surfaced a fatal condition via `record_last_error` (e.g. checkpoint load OOMed)                  | Hit `/readyz?verbose=1` with auth to read `last_error.kind` and `last_error.detail`. Common causes: OOM during checkpoint load (reduce `--medvram` settings), missing CUDA. |
| Probes return 401 instead of 200 / 503            | Orchestrator probe is hitting `/cplugapi/v1/health` (auth-gated) instead of `/cplugapi/v1/livez` or `/cplugapi/v1/readyz` (public) | Switch the probe path. `/livez` for liveness, `/readyz` for readiness. |
| Probes return 200 forever even after pod is wedged | `/livez` is unconditional. Use `/readyz` for "actually serving" signal | Liveness on `/livez` (event loop alive), readiness on `/readyz` (model + drain + last-error checks). |
| Cold boot takes >5 minutes, startup probe fails   | `failureThreshold` too low for checkpoint load                                                            | Set `startupProbe.failureThreshold: 60` with `periodSeconds: 10` (10 minutes). Liveness/readiness aren't gated on startup probe in modern k8s. |
| Drain takes the full 30s every time, gens never finish in grace | Gens are longer than `CPLUG_SHUTDOWN_GRACE_S`                                                | If your typical gen >30s, raise both `CPLUG_SHUTDOWN_GRACE_S` and the orchestrator's `terminationGracePeriodSeconds`. Cap at orchestrator's hard limit (k8s: 60-300s typical). |
| `/sdapi/v1/*` 503 during drain even though it's a metadata read | `RejectDuringDrainMiddleware` rejects POST/PUT/PATCH/DELETE on `/sdapi/v1/{txt2img,img2img}` only. GETs always pass | Check the request method. If it's a POST to a non-gen route, the middleware shouldn't fire — file a bug. |
| Metrics endpoint returns 401                      | `CPLUG_METRICS_PUBLIC` unset (default), Prometheus has no creds                                           | Either set `CPLUG_METRICS_PUBLIC=1` or configure `basic_auth` in the scrape job. |
| Gen latency 10-20× expected, no obvious cause     | NVIDIA driver spilling to shared (PCIe) memory because `peak_vram_mb` ~= total VRAM                       | Check `cplugapi.gen_timing` for `peak_vram_mb`. If it's >90% of card total, disable the spill via `nvidia-smi` or NVIDIA Control Panel. Reduce batch size or add a smaller resolution preset. |
| `Origin not allowed` from your published frontend | `CPLUG_ALLOWED_ORIGINS` not set, cloud profile wildcard accepts any non-empty Origin BUT operator narrowed it | Either re-widen the allow-list or add the frontend's exact origin: `CPLUG_ALLOWED_ORIGINS=https://app.example.com`. |
| Idempotency cache replays mismatched payloads     | Client is reusing the same `Idempotency-Key` for distinct request bodies                                  | This is a client bug. Cplugapi caches first response by key — second request gets the first response back. Generate a fresh ULID per request. |
| ws auth shim returns 403 on legitimate WS upgrade | `--api-auth` set but client doesn't send `Authorization: Basic` on the upgrade request                    | Configure the WS client to set `Authorization` in the upgrade headers (browsers do this for `wss://user:pass@host` URLs; native clients must add the header explicitly). |
| `host_not_allowed` despite `CPLUG_ALLOWED_HOSTS` set | The check is exact-match. `api.example.com` does not match `Host: api.example.com:443`                  | Add the host:port form too: `CPLUG_ALLOWED_HOSTS=api.example.com,api.example.com:443`. Or set wildcard. |

## Migration from desktop loopback to cloud

Switching a working desktop deploy to cloud:

| Variable                          | Desktop value                | Cloud value                              |
|-----------------------------------|------------------------------|------------------------------------------|
| `CPLUG_DEPLOYMENT_PROFILE`        | unset (defaults `desktop`)   | `cloud`                                  |
| `--api-auth`                      | optional (loopback bind)     | required                                 |
| `CPLUG_ALLOWED_HOSTS`             | unset (loopback only)        | wildcard or your DNS hostnames           |
| `CPLUG_ALLOWED_ORIGINS`           | unset (loopback regex)       | wildcard or your frontend origin         |
| `CPLUG_TRUSTED_PROXIES`           | unused                       | required (the ingress's CIDR)            |
| `CPLUG_PREEMPT_MODE`              | `always` (sketch workflow)   | unset → resolves to `off` per profile    |
| `CPLUG_LOG_FORMAT`                | unset (text)                 | `json`                                   |
| `CPLUG_SHUTDOWN_REJECT_NEW`       | unset → `0`                  | unset → resolves to `1` per profile      |
| `CPLUG_SHUTDOWN_GRACE_S`          | unset → `30`                 | `25` (matched to orchestrator + 5s)      |

First-deploy verification checklist:

1. **`/identify` reachable from the public internet** without auth.
   `curl https://api.example.com/cplugapi/v1/identify` returns
   `fork`, `fork_version`, `capabilities[]` containing
   `deployment-profile-cloud`. If `deployment-profile-cloud` is
   missing, the profile env var didn't reach the process.
2. **`/livez` returns 200 unconditional.** Without auth.
3. **`/readyz` returns 200 with `checks.draining: false`** once the
   model is loaded. Without auth. With `?verbose=1` + Basic auth, the
   `last_error` field is `null`.
4. **`/health` returns 401 without creds, 200 with creds.** The
   `capabilities[]` array is the long form (no leak filtering — that's
   only on `/identify`).
5. **`X-RateLimit-Limit`, `-Remaining`, `-Reset` on every response.**
   If absent, rate limiting is disabled (cloud default should set
   `mutating=30`, `read=600`).
6. **`X-RateLimit-Remaining` decrements when you hit from a fresh IP.**
   If two distinct source IPs share one decrementing counter, your
   `CPLUG_TRUSTED_PROXIES` is wrong — every request is keyed on the
   ingress IP because the XFF chain isn't trusted.
7. **`/metrics` exposes the four metric families** named in the
   Prometheus section. Auth-gated by default.
8. **Trigger a graceful shutdown manually** (`kill -TERM <pid>` or
   delete the pod). `/readyz` should flip to 503 with `draining: true`
   within 1 probe interval. New POSTs to gen routes return 503
   `Retry-After: 5`. Existing GETs continue. The process exits
   within `CPLUG_SHUTDOWN_GRACE_S + ε`.
9. **Idempotency replay round-trip.** Send a POST with
   `Idempotency-Key: 01HXXX...`; replay; verify response body is
   identical and `Idempotency-Replayed: true` is on the second
   response. `cplugapi_idempotency_replays_total` increments by 1.
10. **Generate one image, watch logs.** With `CPLUG_LOG_FORMAT=json`
    and `CPLUG_GEN_TIMING=1`, you should see one
    `cplugapi.gen_timing` line per gen with `total_ms`,
    `vae_decode_ms`, `peak_vram_mb`. With `CPLUG_SDAPI_OBSERVER=1`, one
    line per sdapi request.

If any of those fails, work backwards through this runbook before
shipping client traffic. The fork is single-replica, so the cost of
catching a misconfig in production is "every active client is broken
until you fix it" — there is no second replica to soak it.

## See also

- API contract: `doc/cplugapi.md`.
- Threat model: `doc/cplugapi-threat-model.md` (W19, sibling document
  with attack surface and mitigations).
- Plan: local `plan/cplugapi-world-class.md` §1 (non-goals),
  §3 (request lifecycle), W5 / W8 / W9 / W12 work items
  (the `plan/` directory is gitignored — see local working copy).
- OpenAPI spec: generated by `scripts/export_cplugapi_openapi.py`,
  attached as a CI artifact on tag pushes.
