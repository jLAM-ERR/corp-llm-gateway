"""Corp-NER failure type + the error-code helper the hook handlers use.

``CorpNerUnavailableError`` subclasses ``detectors.NerUnavailableError`` on
purpose: the fail-closed ``except NerUnavailableError`` handlers already on the
egress path (``litellm_hook._pre_call_impl`` and
``litellm_hook._sanitize_prompt_field``) then cover every corp-NER failure with
no new call site to miss. ``ner_error_code`` keeps the operational distinction
between a missing local NER model and a corp-NER service outage.
"""

from __future__ import annotations

from corp_llm_gateway.detectors import NerUnavailableError

E_NER_UNAVAILABLE = "E_NER_UNAVAILABLE"
E_CORP_NER_UNAVAILABLE = "E_CORP_NER_UNAVAILABLE"


class CorpNerUnavailableError(NerUnavailableError):
    """The corp NER service could not scan a text — fail closed, never fail open.

    Raised for transport errors, timeouts, non-2xx responses, malformed
    payloads, and for input the client cannot batch within the service limits
    (which would otherwise go partly unscanned).

    Messages carry status codes, counts and exception types only — never
    request or response text (M1-14).
    """


def ner_error_code(exc: BaseException) -> str:
    return E_CORP_NER_UNAVAILABLE if isinstance(exc, CorpNerUnavailableError) else E_NER_UNAVAILABLE
