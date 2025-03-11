from pydantic import BaseModel

class Autocompletion(BaseModel):
    text: str

    class Config:
        extra = "forbid"

class Tags(BaseModel):
    tags: list[str]

    class Config:
        extra = "forbid"

class Summary(BaseModel):
    title: str

    class Config:
        extra = "forbid"