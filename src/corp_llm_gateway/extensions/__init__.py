from corp_llm_gateway.extensions.base import (
    Extension,
    ExtensionKind,
    ExtensionSpec,
)
from corp_llm_gateway.extensions.registry import (
    ExtensionApiVersionError,
    ExtensionRegistry,
)

EXTENSION_API_VERSION = "1"

# Eager module-level singleton is safe ONLY because ExtensionRegistry.__init__
# just allocates empty dicts — no config reads, clients, or I/O at import.
# Discovery of config-defined extensions stays on demand (REGISTRY.discover()).
REGISTRY = ExtensionRegistry()

__all__ = [
    "EXTENSION_API_VERSION",
    "REGISTRY",
    "Extension",
    "ExtensionApiVersionError",
    "ExtensionKind",
    "ExtensionRegistry",
    "ExtensionSpec",
]
