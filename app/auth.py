import logging
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth as firebase_auth
from firebase_admin.exceptions import FirebaseError
from google.auth.exceptions import GoogleAuthError
from pydantic import BaseModel
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)


class StaffIdentity(BaseModel):
    user_id: str
    role: Literal["staff"] = "staff"


def auth_error(status: int, message: str) -> HTTPException:
    headers = {"Cache-Control": "no-store"}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return HTTPException(status_code=status, detail=message, headers=headers)


def require_staff(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> StaffIdentity:
    if credentials is None:
        raise auth_error(401, "Bearer ID token required")

    firebase_app = request.app.state.firebase_app
    if firebase_app is None:
        raise auth_error(503, "Staff authentication is not configured")

    try:
        claims = firebase_auth.verify_id_token(
            credentials.credentials,
            app=firebase_app,
            check_revoked=True,
        )
    except (
        firebase_auth.InvalidIdTokenError,
        firebase_auth.UserDisabledError,
        firebase_auth.UserNotFoundError,
        ValueError,
        TypeError,
    ) as error:
        raise auth_error(401, "Invalid or expired staff session") from error
    except (FirebaseError, GoogleAuthError, RequestException) as error:
        logger.warning("Staff authentication unavailable: %s", type(error).__name__)
        raise auth_error(503, "Staff authentication temporarily unavailable") from error

    if claims.get("staff") is not True:
        raise auth_error(403, "Staff access required")

    return StaffIdentity(user_id=claims["uid"])
