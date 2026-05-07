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
