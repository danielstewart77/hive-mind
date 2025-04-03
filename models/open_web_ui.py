from pydantic import BaseModel, Field

class Autocompletion(BaseModel):
    text: str = Field(..., description="The autocompletion suggestion for the given message")

    class Config:
        extra = "forbid"

class Tags(BaseModel):
    tags: list[str] = Field(..., description="List of tags that categorize the main themes of the chat history")

    class Config:
        extra = "forbid"

class Summary(BaseModel):
    title: str = Field(..., description="The title of the chat thread including an emoji")

    class Config:
        extra = "forbid"