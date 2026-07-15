# Air-gapped / corp-proxy deployment (docker-compose)

How to build and run the docker-compose stack when internet egress is only
allowed through a corporate HTTP(S) proxy and Python packages come from an
internal PyPI mirror.

Guiding principle, same as the rest of the repo: **trust the proxy's root CA**,
never disable TLS verification. The whole air-gapped layer is **optional and
degrades softly** — with every knob empty and no CA supplied, the stack builds
and runs exactly as it does on the open internet.

Scope: this covers the `docker-compose.pilot.yml` stack (the runnable gateway).
The production Helm path has its own egress controls; the same build knobs exist
on `Dockerfile.gateway` for operators who build that image by hand.

## What reaches the internet

| Path | When | Through the proxy? |
| --- | --- | --- |
| `pip install` of the NER stack + spaCy `en_core_web_md` wheel | image **build** (`docker/pilot-litellm/Dockerfile`) | yes |
| `pip install -e /pkg` bootstrap | container **start** (compose `command`) | yes |
| Docker base-image `pull` (`litellm`, `postgres`, `redis`, …) | build/up | yes — **configured on the Docker daemon, not here** (`~/.docker/config.json`) |
| litellm → corp vLLM (`CORP_LLM_ENDPOINT`) | runtime | **no** — internal, kept in `NO_PROXY` |
| redis / postgres | runtime | no — internal, in `NO_PROXY` |

## Where the knobs live

Docker Compose reads `${VAR}` (for `build.args` and the `environment:` block) from
your **shell** or a project-root **`.env`** — *not* from `.env.pilot` (that file is
an `env_file`, injected only into the container, and the `environment:` block would
shadow it). So put the proxy / mirror knobs in a root `.env` (git-ignored) or export
them before `docker compose`.

```
# .env  (project root — git-ignored)
HTTP_PROXY=http://proxy-host:3128
HTTPS_PROXY=http://proxy-host:3128
NO_PROXY=localhost,127.0.0.1,redis,postgres,corp-llm.corp.lan,<docker-subnet>,<internal-domains>
PIP_INDEX_URL=https://mirror-host/artifactory/api/pypi/<repo>/simple
PIP_TRUSTED_HOST=mirror-host          # if the mirror is plain-HTTP or off-proxy-CA
PIP_PROXY=http://pip-proxy-host:3128  # only if pip must use a dedicated proxy node
EN_MODEL_URL=https://mirror-host/.../en_core_web_md-3.8.0-py3-none-any.whl
```

Lowercase `http_proxy` / … are populated automatically inside the containers from
the SAME uppercase source (compose maps both cases), so you only set the uppercase.

### `NO_PROXY` — mandatory

Every intra-stack hop must bypass the proxy, or internal traffic (and internal
credentials) gets routed to it and breaks:

- always include the service names the gateway talks to: `redis`, `postgres`;
- include the corp-vLLM host from `CORP_LLM_ENDPOINT` (default `corp-llm.corp.lan`);
- include `localhost,127.0.0.1` (health checks) and the Docker network subnet;
- the internal PyPI mirror host: either reachable via `PIP_PROXY`, or add it here.

The compose files ship a sane default (`localhost,127.0.0.1,redis,postgres,corp-llm.corp.lan`);
setting `NO_PROXY` in `.env` replaces it — copy those names in.

## Steps

### 1. Drop the re-signing proxy CA (only if the proxy intercepts TLS)

```
crt/proxy-ca.crt      # PEM: root + intermediates of the egress proxy
```

Git-ignored (`crt/*.crt`). `docker/pilot-litellm/Dockerfile` bakes it into the
image's `certifi` and points `pip`/`requests` at it, so build-time pip and the
spaCy download verify against the proxy CA. Absent the file, the image keeps stock
certifi. This is **separate** from `crt/corp-ca-bundle.pem` (the corp-vLLM CA,
mounted at runtime for the upstream TLS). See [../../crt/README.md](../../crt/README.md).

> A plain forwarding proxy (CONNECT tunnel, no re-signing) needs **no CA** — just
> `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`.

### 2. Prepare env

Fill a root `.env` as above, and `.env.pilot` (app config) from `.env.pilot.example`.

### 3. Build

```
docker compose -f docker-compose.pilot.yml build
```

`*_PROXY` are forwarded as Docker predefined build args (scrubbed from image
history); `PIP_INDEX_URL`/`PIP_TRUSTED_HOST`/`PIP_PROXY` become `/etc/pip.conf`;
`EN_MODEL_URL` points the spaCy wheel at internal storage. All empty ⇒ a normal
open-internet build.

### 4. Run

```
docker compose -f docker-compose.pilot.yml up -d
docker compose -f docker-compose.pilot.yml ps
```

At start the litellm container runs `pip install -e /pkg` (through the proxy /
mirror, verified against the proxy CA baked in step 1), then serves the gateway
on :4000 inside the container — reached from the host via nginx on :8080.

## How it works

- **Both-case proxy env from one source.** `HTTP_PROXY` populates the container's
  `HTTP_PROXY` and `http_proxy`, so pip / httpx / aiohttp all honor it.
- **`db` / `redis` get no proxy env** — they have no outbound dependency.
- **CA into `certifi` + `PIP_CERT`.** pip ships its own vendored certifi, so we
  append the proxy CA to the venv certifi AND point `PIP_CERT`/`REQUESTS_CA_BUNDLE`
  at that bundle. Baked at build so both the build and the startup `pip` trust it.
- **Mirror in `pip.conf`.** `PIP_CONFIG_FILE=/etc/pip.conf` persists, so runtime
  pip uses the mirror too.
- **We do NOT set `DISABLE_AIOHTTP_TRANSPORT`.** Unlike a proxy-fronted provider
  setup, the pilot's litellm upstream is the **internal** corp vLLM (in `NO_PROXY`),
  whose TLS is verified via the `SSL_CERT_FILE` combined bundle the compose
  `command` builds. Forcing httpx (certifi-only) there would break that path.

## Troubleshooting

| Symptom | Cause / check |
| --- | --- |
| `Connection error` / TLS failure during **build** | Re-signing proxy but no CA: add `crt/proxy-ca.crt` and rebuild. |
| `pip` can't reach packages during build | `PIP_INDEX_URL` unset (public pypi.org blocked) or `PIP_PROXY`/`HTTP_PROXY` not passed as build args. |
| Container start hangs on `pip install -e /pkg` | Runtime proxy not set in `.env` (shell/root `.env`, not `.env.pilot`), or mirror host not in `NO_PROXY`/`PIP_PROXY`. |
| Internal traffic (redis/postgres/vLLM) routed to proxy | Missing service names / Docker subnet in `NO_PROXY`. |
| Base image won't `pull` | Proxy not configured on the Docker daemon (`~/.docker/config.json`) — outside this repo. |
