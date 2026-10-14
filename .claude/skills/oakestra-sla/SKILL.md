---
name: oakestra-sla
description: Reference for writing Oakestra application deployment SLA descriptors (the JSON file passed to `oak a create` / the dashboard). Use when creating, editing, or reviewing an Oakestra SLA, or porting a Kubernetes/docker-compose service to Oakestra.
---

# Oakestra Application Deployment SLA

Source: https://www.oakestra.io/docs/reference/app-sla/ (extracted 2026-10-08).
Everything under "Documented fields" is from that page. The "Not covered by the docs"
section lists things the page does not cover. Treat those as unverified, and check the
Oakestra repo (github.com/oakestra/oakestra) before relying on them.

The SLA deployment descriptor is a JSON file that describes how an application is
deployed on Oakestra.

## Structure

```json
{
  "sla_version": "v2.0",
  "customerID": "Admin",
  "applications": [
    {
      "applicationID": "",
      "application_name": "X",
      "application_namespace": "default",
      "application_desc": "X application",
      "microservices": [
        {
          "microserviceID": "",
          "microservice_name": "X1",
          "microservice_namespace": "default",
          "virtualization": "container",
          "environment": [],
          "vcpu": 1,
          "storage": 100,
          "code": "docker.io/X/X1",
          "addresses": {
            "rr_ip": "10.30.0.1"
          },
          "constraints": [],
          "volumes": [
            {
              "volume_id": "test",
              "csi_driver": "csi.oakestra.io/hostpath",
              "mount_path": "/app"
            }
          ]
        },
        {
          "microserviceID": "",
          "microservice_name": "X3",
          "microservice_namespace": "default",
          "vcpu": 2,
          "storage": 200,
          "virtualization": "container",
          "code": "docker.io/X/X3",
          "addresses": {
            "rr_ip": "10.30.1.30"
          }
        }
      ]
    }
  ]
}
```

This is the official example with its trailing commas removed. As published it is not
valid JSON, so don't copy it verbatim from the site.

## Documented fields

### Top level
| Field | Meaning |
|---|---|
| `sla_version` | Descriptor format version. Currently `"v2.0"`. |
| `customerID` | User ID. Default `"Admin"`. |
| `applications` | List of applications. One descriptor can define several. |

### Application
The fully qualified app name is `application_name` + `application_namespace`.

| Field | Meaning / rules |
|---|---|
| `application_name` | Unique app name. **Max 10 characters, no symbols.** |
| `application_namespace` | Separates different deployments of the same app, e.g. `default`, `production`, `test`. **Max 10 characters, no symbols.** |
| `applicationID` | Leave `""` for new deployments. Set it only to edit an existing deployment. |
| `application_desc` | Short description. |
| `microservices` | List of the microservices that make up the app. |

### Microservice
The fully qualified service name is `microservice_name` + `microservice_namespace`.

| Field | Meaning / rules |
|---|---|
| `microserviceID` | Leave `""` for new deployments. Set it only to edit an existing deployment. |
| `microservice_name` | **Max 10 characters, no symbols.** |
| `microservice_namespace` | Same idea and limits as the app namespace (**max 10 characters, no symbols**). |
| `virtualization` | `"container"` or `"unikernel"`. Unikernel support starts with Accordion v0.4.301. |
| `code` | Image reference, e.g. `"docker.io/X/X1"`. Shown in the official example; the field list doesn't describe it. |
| `cmd` | List of commands executed inside the container at startup. |
| `vcpu` | Minimum CPU vcores. Note the name is singular `vcpu`. |
| `vgpu` | Minimum GPU vcores. Note the name is singular `vgpu`. |
| `memory` | Minimum memory. The page doesn't state the unit. |
| `storage` | Minimum storage. **The scheduler currently ignores it.** |
| `environment` | List of `KEY=value` strings, e.g. `["VAR=foo"]`. |
| `bandwidth_in` / `bandwidth_out` | Minimum bandwidth on the worker. **The scheduler currently ignores them.** |
| `port` | `hostport_1:containerport_1[/protocol];hostport_2:containerport_2[/protocol]`. Separate multiple mappings with `;`. The default protocol is `tcp`. |
| `addresses.rr_ip` | Optional. A fixed round-robin IP that load-balances across all instances of the service and stays bound to it permanently. **Must be `10.30.x.y`** and must not collide with any other instance address or service IP, or deployment errors. If omitted, the system generates one. |
| `constraints` | Placement constraints (see below). `[]` means none. |
| `volumes` | CSI volume claims (see below). |

### Constraints
The page documents only the `direct` type. It pins the service to one cluster and,
optionally, to a list of eligible nodes:

```json
"constraints": [
  {
    "type": "direct",
    "node": "xavier1",
    "cluster": "gpu"
  }
]
```
- `node`: semicolon-separated node hostnames, e.g. `"node1;node2"`. Only these workers are eligible.
- `cluster`: name of the cluster the service must be scheduled on.

### Volumes
Volumes can only be provisioned by CSI drivers present in your infrastructure, and at
least one cluster must match the chosen `csi_driver`.

```json
"volumes": [
  {
    "volume_id": "<volume_name>",
    "csi_driver": "<name of the CSI driver>",
    "mount_path": "<mount path inside the container>",
    "config": {}
  }
]
```
`config` is optional and takes any extra settings the selected CSI driver allows. For
the hostpath driver's setup, see the CSI section of the `troubleshoot-oakestra` skill.

## Verified by deployment (oak-go-cli v0.6.0, single-machine setup, docker runtime, 2026-10-08)
- **`cmd` replaces the image ENTRYPOINT.** It becomes the full process argv.
  `["sh","-c","..."]` worked on both `ollama/ollama` (entrypoint `/bin/ollama`) and
  `berriai/litellm`. It's a good way to write a config file at startup and then `exec`
  the real binary.
- **Services reach each other over `rr_ip` with no `port` entry.** LiteLLM reached
  Ollama at `http://10.30.10.1:11434`, and the Ollama service had no `port` field.
- **A service with a published `port` is NOT reachable from other services over the
  overlay.** In a probe from inside the overlay, Ollama (no `port`) answered, while
  `llmproxy` (`4000:4000`) and `agent` (`8000:8000`) timed out on both their RR IP and
  their instance IP. Publish a port **only on the public entry point**, and keep every
  service other services call unpublished. The docs don't mention this, so re-check it
  on newer versions.
- **`port` publishes on the worker's IP, not on loopback.** `"4000:4000"` answered on
  `192.168.0.108:4000`, while `127.0.0.1:4000` timed out even on the worker itself.
- CLI: `oak app create -f sla.json -d` creates and deploys the app.
  `oak s ls` shows status. `oak s logs <service-id> <instance>` shows logs (the
  instance number is required).

## Networking (semantic addressing)
Sources: https://www.oakestra.io/docs/concepts/networking/ and
https://www.oakestra.io/docs/manuals/networking-internals/load-balancing/ (and the
`ipv4-addressing` and `proxy-conversion` pages next to them).
- Each service gets one **Service IP per balancing policy**. NetManager's proxy on the
  worker translates it to a live instance's namespace IP on each request, so it stays
  valid across scaling and migration. This is like a Kubernetes ClusterIP, but with one
  address per policy.
- **Round Robin** (`rr_ip`, IPv6 `rr_ip_v6` in `fdff:2000::/21`) is the only policy
  implemented. **Closest** (`fdff:1000::/21`) is *"reserved, but not implemented"*. In
  IPv4 both share `10.30.0.0/16`.
- Each instance also has a stable **Instance IP** (in `10.30.0.0/16`), which follows the
  instance if it migrates. Workers get a `/26` from `10.18.0.0/16` for namespace IPs,
  which apps should never use directly.
- **There's no DNS or name-based discovery.** Callers use the IP. To wire service A to
  service B, pin B's `rr_ip` in the SLA and pass it to A by env var, for example
  `B_URL=http://10.30.10.2:4000`. The alternative is to omit it, deploy, and read the
  assigned IP back from the API.
- Supported: any TCP or UDP protocol. Not supported: translating between IPv4 and IPv6.
- `rr_ip_v6` appears in the internals examples but not in the SLA reference.

## Not covered by the docs (verify before relying on)
- **Units** for `memory` and `storage`. They are probably MB, but the page doesn't say.
- **Fields some SLAs use that this page doesn't list:** `vcpus`/`vgpus`/`vtpus` (plural),
  `added_files`, `state`, `one_shot`, `privileged`. Old examples and the dashboard may
  emit them. Don't add them unless you've checked that your Oakestra version uses them.
- **Secrets.** The page has no secret mechanism, so anything in `environment` is stored
  in the SLA as plain text.
- **Config files.** There is no ConfigMap equivalent on this page. Bake the config into
  the image, write it at startup through `cmd`, or mount it with a CSI volume.
- **`vcpu` reaching the worker.** With `"vcpu": 1`/`2` in the SLA, the job the
  NodeEngine received logged `Vcpus:0` (Memory came through as 2048). Either the CPU
  request isn't passed to the worker or it isn't enforced there. Don't count on a CPU limit.

## Checklist when writing an SLA
1. All names and namespaces are ≤ 10 characters, alphanumeric only (no `-`, `_`, `.`).
2. `applicationID` and `microserviceID` are `""` for a new deployment.
3. Use `vcpu`/`vgpu` (singular), not `vcpus`/`vgpus`.
4. `port` uses `host:container[/proto]` and separates mappings with `;`.
5. Any `rr_ip` is in `10.30.x.y` and is unique. Dependents get it through env vars.
6. Only the public entry point has a `port`. Services that other services call have none.
7. The JSON parses: `python3 -m json.tool file.json` (the official example doesn't).
