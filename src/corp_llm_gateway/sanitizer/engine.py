import logging
from collections.abc import Sequence

from corp_llm_gateway.sanitizer.placeholder import sort_placeholders_by_descending_length
from corp_llm_gateway.sanitizer.strategies import (
    SanitizerStrategy,
    StrategyError,
    StrategyResult,
)

logger = logging.getLogger(__name__)


class AllStrategiesFailedError(Exception):
    pass


class CorpLlmSanitizer:
    """Three-tier sanitization output strategy: function-call → JSON → regex.

    Plan ref: M1-5. Tries each strategy in order, returning the first that
    successfully extracts a `StrategyResult` from the corp LLM's raw output.
    Strategy errors (StrategyError) are logged structurally and the next
    strategy is attempted; only if ALL strategies fail does the call raise.

    NotImplementedError from a strategy is treated as "this strategy is a
    stub and should be skipped entirely" — important for the rev-3 baseline
    where strategies are wired before the corp-LLM endpoint is available.
    """

    def __init__(self, strategies: Sequence[SanitizerStrategy]) -> None:
        if not strategies:
            raise ValueError("CorpLlmSanitizer requires at least one strategy")
        self._strategies = tuple(strategies)

    async def extract(self, raw_llm_output: str) -> StrategyResult:
        last_error: Exception | None = None
        for strategy in self._strategies:
            try:
                result = await strategy.extract(raw_llm_output)
                logger.info(
                    "strategy_succeeded name=%s pairs=%d",
                    strategy.name,
                    len(result.pairs),
                )
                return result
            except NotImplementedError:
                logger.debug("strategy_skipped name=%s", strategy.name)
                continue
            except StrategyError as exc:
                last_error = exc
                logger.info(
                    "strategy_failed name=%s exception=%s",
                    strategy.name,
                    type(exc).__name__,
                )
                continue
        logger.warning(
            "strategy_all_failed strategies=%d last=%s",
            len(self._strategies),
            type(last_error).__name__ if last_error else "None",
        )
        raise AllStrategiesFailedError(
            f"all {len(self._strategies)} strategies failed; last={last_error!r}"
        )

    def apply(
        self,
        text: str,
        mapping: StrategyResult,
    ) -> str:
        """Substitute placeholders into text in length-descending order (M1-9)."""
        by_original = {original: placeholder for original, placeholder in mapping.pairs}
        for original in sort_placeholders_by_descending_length(by_original):
            text = text.replace(original, by_original[original])
        return text

    def reverse(
        self,
        text: str,
        mapping: StrategyResult,
    ) -> str:
        """Reverse: replace placeholders with originals, length-descending."""
        by_placeholder = {placeholder: original for original, placeholder in mapping.pairs}
        for placeholder in sort_placeholders_by_descending_length(by_placeholder):
            text = text.replace(placeholder, by_placeholder[placeholder])
        return text
