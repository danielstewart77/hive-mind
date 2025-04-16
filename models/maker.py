from pydantic import BaseModel

class AgentName(BaseModel):
    name: str

    class Config:
        extra = "forbid"

class AgentCode(BaseModel):
    code: str
    class Config:
        extra = "forbid"

class RequirementUpdate(BaseModel):
    update: bool
    requirements: list[str]

    class Config:
        extra = "forbid"

class RequiredLibrariesUpdate(BaseModel):
    update: bool
    libraries: list[str]

    class Config:
        extra = "forbid"