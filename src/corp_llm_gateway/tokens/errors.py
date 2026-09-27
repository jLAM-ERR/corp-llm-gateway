class AuthError(Exception):
    pass


class MissingTokenError(AuthError):
    pass


class InvalidTokenError(AuthError):
    pass


class ExpiredTokenError(AuthError):
    pass


class RevokedTokenError(AuthError):
    pass


class IssuancePolicyError(Exception):
    """Issuance refused by the per-subject policy. Args carry the code only (M1-14).

    The route maps RATE and REPLAY to 403, and BUSY (the store's per-subject lock
    wait timed out) to 503.
    """

    RATE = "E_ISSUE_RATE"
    REPLAY = "E_ISSUE_REPLAY"
    BUSY = "E_ISSUE_BUSY"

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
