# Pilot-only image: litellm v1.85 + the corp-llm-gateway `ner` extra baked in
# (Natasha RU + spaCy EN + en_core_web_md), so the pilot runs FULL bilingual NER
# (ФИО / organisations) instead of lazily degrading to the regex floor.
#
# Base is Python 3.13 (litellm's own /app/.venv). Validated live: this NER stack
# resolves and coexists with litellm's numpy/pydantic without downgrading them.
# The spaCy EN model is a py3-none-any wheel fetched by URL (it is not on PyPI by
# name, and `python -m spacy download` needs a pip the uv image may lack).
FROM docker.litellm.ai/berriai/litellm:v1.85.0

# ── Air-gapped / re-signing-proxy support ────────────────────────────────────
# All knobs are empty by default, so an open-internet build works UNCHANGED.
# A re-signing corporate proxy re-signs outbound HTTPS, so build-time pip + the
# spaCy model download must TRUST the proxy's root CA (not disable TLS). Single
# source of truth: crt/proxy-ca.crt (git-ignored via crt/*.crt — see crt/README.md).
# The whole crt/ dir is COPYed (it always exists — has README.md), so the build
# ALSO works with NO proxy CA supplied: the append is skipped and stock certifi
# stays. crt/ here is the EGRESS-proxy CA; the corp-vLLM CA (crt/*.pem) is a
# separate, runtime-mounted bundle (SSL_CERT_FILE, built by the compose `command`).
COPY crt/ /tmp/proxy-crt/
RUN set -eux; \
    certbundle="$(/app/.venv/bin/python -c 'import certifi; print(certifi.where())')"; \
    if [ -f /tmp/proxy-crt/proxy-ca.crt ]; then \
        cat /tmp/proxy-crt/proxy-ca.crt >> "$certbundle"; \
    fi; \
    mkdir -p /etc/corp-llm-gateway; \
    cp "$certbundle" /etc/corp-llm-gateway/ca-bundle.pem; \
    rm -rf /tmp/proxy-crt
# pip ships its OWN vendored certifi, so appending to `import certifi` above is not
# enough — point pip/requests at the bundle we appended the proxy CA to. The file
# always exists (stock certifi at minimum), so this is safe on open-internet builds.
ENV PIP_CERT=/etc/corp-llm-gateway/ca-bundle.pem \
    REQUESTS_CA_BUNDLE=/etc/corp-llm-gateway/ca-bundle.pem

# Internal PyPI mirror (Artifactory/Nexus). The re-signing proxy usually blocks
# CONNECT tunnels to public pypi.org, so pip must use the mirror instead. Written
# to /etc/pip.conf + PIP_CONFIG_FILE so BOTH the build-time installs below AND the
# runtime `pip install -e /pkg` (compose `command`) use it. Empty => public PyPI.
# PIP_TRUSTED_HOST for a mirror on plain HTTP or a cert outside the proxy CA;
# PIP_PROXY for a pip-only proxy node (overrides the generic *_PROXY for pip).
ARG PIP_INDEX_URL=""
ARG PIP_TRUSTED_HOST=""
ARG PIP_PROXY=""
RUN set -eux; \
    printf '[global]\n' > /etc/pip.conf; \
    if [ -n "$PIP_INDEX_URL" ]; then printf 'index-url = %s\n' "$PIP_INDEX_URL" >> /etc/pip.conf; fi; \
    if [ -n "$PIP_TRUSTED_HOST" ]; then printf 'trusted-host = %s\n' "$PIP_TRUSTED_HOST" >> /etc/pip.conf; fi; \
    if [ -n "$PIP_PROXY" ]; then printf 'proxy = %s\n' "$PIP_PROXY" >> /etc/pip.conf; fi
ENV PIP_CONFIG_FILE=/etc/pip.conf

# spaCy EN model wheel. Empty => the public GitHub release; set it to the wheel on
# your internal mirror/storage for an air-gapped build.
ARG EN_MODEL_URL=""

# litellm's /app/.venv is a uv venv with NO pip — bootstrap it first (the pilot's
# runtime `command` does the same via ensurepip before installing the project).
# HTTP_PROXY/HTTPS_PROXY/NO_PROXY are Docker predefined build args (forwarded from
# docker-compose build.args) — BuildKit injects them into RUN and scrubs them from
# the final image, so pip reaches the mirror/GitHub through the proxy at build time.
RUN set -eux; \
    /app/.venv/bin/python -m ensurepip --upgrade; \
    /app/.venv/bin/python -m pip install --no-cache-dir \
      natasha slovnet navec razdel pymorphy3 "spacy>=3.8,<3.9"; \
    en_model="${EN_MODEL_URL:-https://github.com/explosion/spacy-models/releases/download/en_core_web_md-3.8.0/en_core_web_md-3.8.0-py3-none-any.whl}"; \
    /app/.venv/bin/python -m pip install --no-cache-dir "$en_model"; \
    /app/.venv/bin/python -c "import spacy; spacy.load('en_core_web_md'); import natasha; import litellm; print('pilot image: NER + litellm OK')"
