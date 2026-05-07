from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TokenInfo:
    corp_token: str
    user_id: str
    team_id: str
    scopes: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
