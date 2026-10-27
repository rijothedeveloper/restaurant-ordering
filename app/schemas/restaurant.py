from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class Restaurant(BaseModel):
    id: UUID
    name: str
    created_at: datetime
