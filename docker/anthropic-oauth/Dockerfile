# Anthropic subscription (OAuth) passthrough. Kept as its own profile because the
# route is native `anthropic/` only — the pinned GLM demo image routes everything
# to the corp vLLM and would never reach litellm's Anthropic OAuth branch.
FROM ghcr.io/berriai/litellm:v1.89.3

# Match the demo's bilingual local NER coverage so changing the upstream model
# does not silently weaken PERSON/ORGANIZATION detection.
RUN /app/.venv/bin/python -m ensurepip --upgrade \
 && /app/.venv/bin/python -m pip install --no-cache-dir \
      natasha slovnet navec razdel pymorphy3 "spacy>=3.8,<3.9" \
 && /app/.venv/bin/python -m pip install --no-cache-dir \
      "https://github.com/explosion/spacy-models/releases/download/en_core_web_md-3.8.0/en_core_web_md-3.8.0-py3-none-any.whl" \
 && /app/.venv/bin/python -c "import spacy; spacy.load('en_core_web_md'); import natasha; import litellm; print('anthropic-oauth image: NER + litellm OK')"
