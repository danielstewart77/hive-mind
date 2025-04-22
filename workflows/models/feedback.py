from pydantic import BaseModel, Field


class UserFeedback(BaseModel):
    approve: bool = Field(..., description="""This value is derived from the user's feedback.""")
    user_feedback: str = Field(..., description="""The user's feedback on the code. This value MUST 
                               come VERBATIM from the user's message. DO NOT change this in ANY way. """)

    class Config:
        extra = "forbid"