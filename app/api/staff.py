from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.auth import StaffIdentity, require_staff

router = APIRouter(
    prefix="/staff",
    tags=["staff"],
    dependencies=[Depends(require_staff)],
)


@router.get("/me", response_model=StaffIdentity)
async def staff_me(
    response: Response,
    staff: Annotated[StaffIdentity, Depends(require_staff)],
) -> StaffIdentity:
    response.headers["Cache-Control"] = "no-store"
    return staff
