# ChatGPT subscription passthrough requires LiteLLM's current Responses API
# implementation. Keep this profile separate from the pinned GLM demo image.
FROM ghcr.io/berriai/litellm:v1.89.3

# Match the demo's bilingual local NER coverage so changing the upstream model
# does not silently weaken PERSON/ORGANIZATION detection.
RUN /app/.venv/bin/python -m ensurepip --upgrade \
 && /app/.venv/bin/python -m pip install --no-cache-dir \
      natasha slovnet navec razdel pymorphy3 "spacy>=3.8,<3.9" \
 && /app/.venv/bin/python -m pip install --no-cache-dir \
      "https://github.com/explosion/spacy-models/releases/download/en_core_web_md-3.8.0/en_core_web_md-3.8.0-py3-none-any.whl" \
 && /app/.venv/bin/python -c "import spacy; spacy.load('en_core_web_md'); import natasha; import litellm; print('chatgpt-codex image: NER + litellm OK')"
