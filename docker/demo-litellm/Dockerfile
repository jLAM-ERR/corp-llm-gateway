# Demo-only image: litellm v1.85 + the corp-llm-gateway `ner` extra baked in
# (Natasha RU + spaCy EN + en_core_web_md), so the demo runs FULL bilingual NER
# (ФИО / organisations) instead of lazily degrading to the regex floor.
#
# Base is Python 3.13 (litellm's own /app/.venv). Validated live: this NER stack
# resolves and coexists with litellm's numpy/pydantic without downgrading them.
# The spaCy EN model is a py3-none-any wheel fetched by URL (it is not on PyPI by
# name, and `python -m spacy download` needs a pip the uv image may lack).
FROM ghcr.io/berriai/litellm:v1.85.0

# litellm's /app/.venv is a uv venv with NO pip — bootstrap it first (the demo's
# runtime `command` does the same via ensurepip before installing the project).
RUN /app/.venv/bin/python -m ensurepip --upgrade \
 && /app/.venv/bin/python -m pip install --no-cache-dir \
      natasha slovnet navec razdel pymorphy3 "spacy>=3.8,<3.9" \
 && /app/.venv/bin/python -m pip install --no-cache-dir \
      "https://github.com/explosion/spacy-models/releases/download/en_core_web_md-3.8.0/en_core_web_md-3.8.0-py3-none-any.whl" \
 && /app/.venv/bin/python -c "import spacy; spacy.load('en_core_web_md'); import natasha; import litellm; print('demo image: NER + litellm OK')"
