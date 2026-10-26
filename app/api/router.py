from fastapi import APIRouter

router = APIRouter()


@router.get("/", tags=["system"])
async def root() -> dict[str, str]:
    return {
        "service": "restaurant-ordering",
        "message": "API is running",
    }