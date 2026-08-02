# litellm resolves litellm_settings.callbacks as a FILE under the config dir
# (<config-dir>/corp_llm_gateway/bootstrap.py), never via importlib — this
# shim satisfies that lookup and delegates to the real corp_llm_gateway.bootstrap
# installed in the image's site-packages, so compose never drifts from the
# wheel. Same technique as helm/corp-llm-gateway/templates/configmap-litellm.yaml
# and examples/compose/docker-compose.yml.
from corp_llm_gateway.bootstrap import guardrail  # noqa: F401
