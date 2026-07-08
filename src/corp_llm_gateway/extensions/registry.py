from collections.abc import Callable

from corp_llm_gateway.extensions.base import Extension, ExtensionKind, ExtensionSpec
from corp_llm_gateway.healthz import HealthStatus

ExtensionFactory = Callable[[], Extension]


class ExtensionApiVersionError(RuntimeError):
    """A registered extension declares an ``api_version`` incompatible with the
    core; the registry refuses to load it (fails closed)."""


class ExtensionRegistry:
    def __init__(self) -> None:
        self._specs: dict[tuple[str, str], ExtensionSpec] = {}
        self._factories: dict[tuple[str, str], ExtensionFactory] = {}

    def register(self, spec: ExtensionSpec, factory: ExtensionFactory) -> None:
        key = (spec.kind, spec.name)
        self._specs[key] = spec
        self._factories[key] = factory

    def get(self, kind: ExtensionKind, name: str) -> Extension:
        factory = self._factories.get((kind, name))
        if factory is None:
            raise ValueError(f"Unknown extension {kind}:{name!r}; expected one of {self._known()}")
        return factory()

    def enabled(self, kind: ExtensionKind) -> tuple[Extension, ...]:
        # No enable/disable state yet; every registered impl of the kind counts.
        return tuple(factory() for (k, _name), factory in self._factories.items() if k == kind)

    async def health_all(self) -> dict[str, HealthStatus]:
        report: dict[str, HealthStatus] = {}
        for (kind, name), factory in self._factories.items():
            try:
                report[f"{kind}:{name}"] = await factory().health()
            except Exception as exc:  # a flapping extension must not crash aggregation
                report[f"{kind}:{name}"] = HealthStatus(False, f"health_error:{type(exc).__name__}")
        return report

    def validate_api_version(self, core_api_version: str) -> None:
        for spec in self._specs.values():
            if spec.api_version != core_api_version:
                raise ExtensionApiVersionError(
                    f"extension {spec.kind}:{spec.name} declares api_version="
                    f"{spec.api_version!r}, incompatible with core {core_api_version!r}"
                )

    def discover(self) -> None:
        """On-demand seam for config-driven registration; reads nothing at import."""

    def _known(self) -> tuple[str, ...]:
        return tuple(sorted(f"{kind}:{name}" for kind, name in self._factories))
