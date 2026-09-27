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
    """Issuance refused by the per-subject policy (403). Args carry the code only (M1-14)."""

    RATE = "E_ISSUE_RATE"
    REPLAY = "E_ISSUE_REPLAY"

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
