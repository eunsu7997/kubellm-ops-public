# Docker GPU security migration (prepared, NOT VERIFIED at runtime)

This guide is a future maintenance procedure. No running container or network was
changed while preparing it. The old Docker GPU/E2E evidence remains valid for the
old topology; it does not prove the new Compose deployment works.

## Current and proposed topology

Read-only Docker inspection on 2026-10-11 confirmed the old vLLM and Gateway use
the default `bridge`; vLLM has an unspecified HostIp on host port 8002, while
Gateway publishes only 127.0.0.1:8003. Saved evidence confirms all GPU devices and
Qwen/Qwen2.5-0.5B-Instruct with utilization 0.4 and max length 2048.

`docker-compose.vllm-secure.yml` is standalone. Its Compose-owned network
`kubellm-gpu-secure_backend` is a user-defined bridge. Gateway connects through
Docker DNS `http://vllm:8000`; Prometheus scrapes `gateway:8000` and `vllm:8000`;
Grafana uses `http://prometheus:9090`. vLLM has **no published host port**.
Gateway/Prometheus/Grafana bind only to localhost on 18003/19091/13001.
The optional override publishes vLLM only at 127.0.0.1:18002.

The exact validated image digest, model, GPU Count=-1 equivalent (`count: all`),
model options are preserved. Host IPC matches the preparation script, but the old
container IPC mode is not proven by saved inspection evidence. All visible GPUs would be
allocated; on the observed machine that is the RTX 4060. Host IPC is retained
for compatibility and is not a new isolation guarantee. This bridge is not
`internal: true`: model downloads require outbound connectivity. No Docker
socket is mounted. Private networking is not API authentication.

## Prerequisites and static checks

- Obtain explicit approval for a maintenance window and service/network changes.
- Check Docker GPU access, driver compatibility, free VRAM and available disk.
  Do not run two model servers simultaneously on this 8GB GPU by default.
- Ensure project `kubellm-gpu-secure`, its network/volumes and ports are unused.
  Abort on a collision rather than adopting or deleting existing resources.
  The `kubellm-gpu-example` stack (`docker-compose.vllm-example.yml`) uses the
  same host ports: 18002/18003/19091/13001. It must not be running during this
  migration; if it is running, defer migration until separately approved handling.
- Preserve old containers and their model cache; never use `docker rm`, prune,
  or `down -v` during this transition. The new named model cache is separate and
  may require a fresh model download. Model Hub revision remains unpinned.
- Prepare the Gateway image **before** stopping anything. Preserve its image ID.
- Use a session-local Grafana password. Do not print the rendered configuration,
  full container inspect, shell environment, or password into evidence.

PowerShell, from the repository root (config/build do not start services):

```powershell
$grafanaSecret = Read-Host 'Grafana password' -AsSecureString
$env:KUBELLM_SECURE_GRAFANA_PASSWORD = [System.Net.NetworkCredential]::new('', $grafanaSecret).Password
docker compose -f docker-compose.vllm-secure.yml config --quiet
docker compose -f docker-compose.vllm-secure.yml -f docker-compose.vllm-secure-localhost.yml config --quiet
docker compose -f docker-compose.vllm-secure.yml build gateway
```

Compose environment variables are inspectable by local Docker administrators.
The password is not committed; it initializes the new Grafana data volume.
Changing the variable does not reset an existing Grafana database password.
Before the Stage 3 edits, both secure `config --quiet` variants were reported
as passing; those results do not validate the current files. Re-checking the
current secure file, its localhost override, example and mock Compose is
**NOT VERIFIED — USER VERIFICATION REQUIRED**. The saved
audit checks (비공개 원본 `evidence/audit-followup-20261011/static-checks.txt`) cover
only earlier mock/example versions, also edited in Stage 3. Rendered configuration
was checked in memory (not printed or saved): the GPU count normalizes to -1,
all four services use the backend bridge, base vLLM has no published port, and
the optional publication binds only to 127.0.0.1. A disposable interpolation
value was used for static validation only, not as a deployed Grafana password.
The existing pytest suite passed (10 tests; 54 dependency deprecation warnings).
Those preparation results were agent-reported without a saved evidence file.
These tests use in-process/mock clients and do not verify this Docker topology.
Build, container creation and network creation were not executed.

## Approved replacement procedure (do not run before approval)

1. Record old container IDs, image IDs, selected command/port/network fields and
   HTTP readiness in a **new** evidence directory. Keep existing evidence intact.
   Record both legacy restart policies using selected fields only:

```powershell
docker inspect kubellm-vllm --format '{{.HostConfig.RestartPolicy.Name}}'
docker inspect kubellm-gateway-vllm-20261011 --format '{{.HostConfig.RestartPolicy.Name}}'
```

   If either policy is not `no`, pause before stopping or starting services.
   Obtain separate explicit approval for any `docker update` policy change and
   re-inspect both policies afterward. In particular, `always` can revive the
   public port 8002 and compete for VRAM after a Docker Desktop restart or reboot.
   The secure stack deliberately has no restart policy: it will not automatically
   recover after reboot. Its recovery requires a separately approved start and
   the same legacy-policy, port and VRAM checks.

2. Keep the existing link unchanged until maintenance: old Gateway continues to
   use `host.docker.internal:8002`. Do not change just one end of that link or
   connect live containers to new networks during preparation.
3. Stop only the two named old application containers; retain them for rollback:

```powershell
docker stop kubellm-gateway-vllm-20261011
docker stop kubellm-vllm
nvidia-smi
```

Before starting the new model server, confirm that the old vLLM GPU process has
exited and its VRAM allocation has been released. Check for other GPU workloads
and sufficient free VRAM for the configured 0.4 share (about 3.2 GiB on 8 GiB).
If memory is still occupied or cannot be confirmed, pause here; do not start a
second model server or stop unrelated GPU workloads. If startup later reports
insufficient memory, save the logs and follow the approved rollback decision.

```powershell
docker compose -f docker-compose.vllm-secure.yml up -d vllm
docker compose -f docker-compose.vllm-secure.yml logs --tail 80 vllm
docker compose -f docker-compose.vllm-secure.yml exec -T vllm python3 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/v1/models', timeout=5).read().decode())"
```

Poll the model query with a bounded timeout (up to 20 minutes). Require HTTP 200
and the Qwen model ID, then start the remaining services:

```powershell
docker compose -f docker-compose.vllm-secure.yml up -d gateway prometheus grafana
Invoke-RestMethod http://127.0.0.1:18003/healthz
Invoke-RestMethod http://127.0.0.1:18003/readyz
# Require both targets UP and both counters present before taking the baseline.
Invoke-RestMethod http://127.0.0.1:19091/api/v1/targets
$counterQuery = [uri]::EscapeDataString('sum by (__name__) ({__name__=~"gateway_requests_total|vllm:generation_tokens_total"})')
$counterUrl = "http://127.0.0.1:19091/api/v1/query?query=$counterQuery"
$before = Invoke-RestMethod $counterUrl
$before.data.result | Select-Object metric, value
$body = @{messages=@(@{role='user';content='Say hello briefly.'});max_tokens=16;temperature=0} | ConvertTo-Json -Depth 5
Invoke-RestMethod http://127.0.0.1:18003/v1/chat/completions -Method Post -ContentType application/json -Body $body
docker compose -f docker-compose.vllm-secure.yml logs --tail 50 gateway
Invoke-RestMethod http://127.0.0.1:19091/api/v1/targets
```

Require actual Qwen assistant text, backend HTTP 200, target UP, and metric
increments. `/readyz` alone only proves Gateway client initialization.
After the chat, poll the query below for up to 60 seconds (scrape interval is
5 seconds). Require `status=success`, both named counter series present before
and after, and both numeric values increasing. Empty results are not zero and
are a failed verification; investigate scrape targets or missing metrics.

```powershell
$after = Invoke-RestMethod $counterUrl
$after.data.result | Select-Object metric, value
Invoke-RestMethod http://127.0.0.1:13001/api/health
# Use the password that initialized this Grafana volume; do not print it.
$grafanaCredential = Get-Credential -UserName admin -Message 'Secure Grafana login'
$grafanaAuth = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(
    ('{0}:{1}' -f $grafanaCredential.UserName, $grafanaCredential.GetNetworkCredential().Password)))
try {
    $dashboards = Invoke-RestMethod 'http://127.0.0.1:13001/api/search?type=dash-db' -Headers @{Authorization="Basic $grafanaAuth"}
    $dashboards | Where-Object uid -eq 'kubellm-ops' | Select-Object uid, title, url
} finally {
    Remove-Variable grafanaAuth, grafanaCredential -ErrorAction SilentlyContinue
}
```

Require Grafana health `database=ok` and exactly one dashboard with UID
`kubellm-ops`. Open `http://127.0.0.1:13001/d/kubellm-ops` and verify the Gateway
and vLLM panels render with the provisioned Prometheus datasource and fresh data.
API health/search alone do not prove dashboard rendering. Do not record
credentials or Authorization headers in evidence.

Inspect **selected fields** to verify vLLM PortBindings is empty and both apps
are on `kubellm-gpu-secure_backend`:

```powershell
$newVllmId = docker compose -f docker-compose.vllm-secure.yml ps -q vllm
$newGatewayId = docker compose -f docker-compose.vllm-secure.yml ps -q gateway
docker inspect $newVllmId --format '{{json .HostConfig.PortBindings}} {{json .NetworkSettings.Networks}}'
docker inspect $newGatewayId --format '{{json .HostConfig.PortBindings}} {{json .NetworkSettings.Networks}}'
docker inspect kubellm-vllm --format '{{.State.Running}}'
Get-NetTCPConnection -State Listen | Where-Object LocalPort -eq 8002
```

The old container must report false and the port query must show no listener.
Include the override file in Compose commands if it was used; in that case the
new vLLM PortBindings must contain only 127.0.0.1:18002. Also check IPv6 bindings.
Do not claim security remediation before these checks.
Clients must switch from port 8003 to 18003. Old monitoring still points to the
old ports; use the new monitoring at 19091/13001 instead. This causes a planned
outage; it is not a zero-downtime cutover.

For optional localhost debugging, use **both** files consistently in up/ps/stop:

```powershell
docker compose -f docker-compose.vllm-secure.yml -f docker-compose.vllm-secure-localhost.yml up -d
Invoke-RestMethod http://127.0.0.1:18002/v1/models
```

Do not run that optional command on a live deployment without approval: it can
recreate the vLLM container. Never add a bare `8002:8000` mapping.

## Rollback (future approval required)

If readiness/E2E fails, save new logs first. Stop only the new project; do not
delete volumes, networks, old containers or evidence:

```powershell
docker compose -f docker-compose.vllm-secure.yml stop
nvidia-smi
```

If the localhost override was used, include it in the stop command too. A stop
preserves model cache, Grafana state and network for diagnosis/retry.
Before restarting the legacy model server, confirm the new vLLM GPU process has
exited and its VRAM allocation has been released, with sufficient free VRAM for
the old 0.4 share. If memory is still occupied or cannot be confirmed, pause;
do not start another model server or stop unrelated GPU workloads.

**Legacy service recovery restores the old insecure vLLM port binding.** Obtain
explicit acceptance of that exposure before the following rollback. If it is
unacceptable, keep the old service stopped and repair/retry the secure project.
The current old container cannot have its immutable PortBindings changed by
`docker start`.

```powershell
docker start kubellm-vllm
# Wait for the old model endpoint to return the Qwen ID before starting Gateway.
Invoke-RestMethod http://127.0.0.1:8002/v1/models
docker start kubellm-gateway-vllm-20261011
Invoke-RestMethod http://127.0.0.1:8003/healthz
$body = @{messages=@(@{role='user';content='Say hello briefly.'});max_tokens=16;temperature=0} | ConvertTo-Json -Depth 5
Invoke-RestMethod http://127.0.0.1:8003/v1/chat/completions -Method Post -ContentType application/json -Body $body
```

Repoint clients to old port 8003 only after E2E succeeds. Retained old monitoring
can resume scraping the old endpoints. Remove the password environment variable
from the PowerShell session after operations using
`Remove-Item Env:KUBELLM_SECURE_GRAFANA_PASSWORD`.

## Verification boundary

Prepared: digest pinning, user-defined bridge, Docker DNS, unpublished vLLM port,
optional localhost-only publication and session-local Grafana password.
NOT VERIFIED: creation of this network, GPU allocation in this Compose project,
new topology model load/E2E, port closure, new monitoring, cutover and rollback.
Kubernetes GPU scheduling, DCGM and performance/stress tests remain NOT VERIFIED.
