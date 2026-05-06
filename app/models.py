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


class DriveAccountPayload(BaseModel):
    name: str
    provider: str = Field(pattern="^(quark|uc|baidu|ali|115|other)$")
    enabled: bool = True
    sortOrder: int = 0
    cookie: str = ""
    token: str = ""
    userAgent: str = ""
    notes: str = ""


class DrivePlayNormalizePayload(BaseModel):
    payload: Any


class QuarkSharePayload(BaseModel):
    shareURL: str


class QuarkFileListPayload(BaseModel):
    shareURL: str
    pdirFid: str = "0"


class QuarkPlayPayload(BaseModel):
    shareURL: str
    fid: str
    flag: str = ""
    getTranscodeUrls: bool = True
