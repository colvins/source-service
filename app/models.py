from typing import Any

from pydantic import BaseModel, Field


class SourcePayload(BaseModel):
    name: str
    runtimeType: str = Field(pattern="^(javascript|python)$")
    enabled: bool = True
    sortOrder: int = 0
    tags: list[str] = []
    notes: str = ""
    description: str = ""
    version: str = ""
    downloadURL: str = ""
    dependencies: list[str] = []
    scriptContent: str = ""


class SubscriptionPayload(BaseModel):
    name: str
    enabled: bool = True
    sortOrder: int = 0
    notes: str = ""
    sourceIds: list[int] = []


class SettingPayload(BaseModel):
    value: Any
