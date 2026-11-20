import hmac
import os

from fastapi import HTTPException, Request

from litellm.proxy._types import UserAPIKeyAuth


async def authenticate(request: Request, api_key: str | None) -> UserAPIKeyAuth:
    expected = os.environ.get("ANTIGRAVITY_PROXY_KEY", "")
    supplied = (api_key or "").removeprefix("Bearer ")
    if not expected or not hmac.compare_digest(expected.encode(), supplied.encode()):
        raise HTTPException(status_code=401, detail="Invalid Antigravity proxy API key")
    return UserAPIKeyAuth(api_key=supplied, user_role="proxy_admin")
