"""User model. Deliberately written against the Pydantic v1 API."""

from pydantic import BaseModel, validator


class User(BaseModel):
    name: str
    email: str

    @validator("email")
    def email_has_at(cls, value: str) -> str:
        if "@" not in value:
            raise ValueError("invalid email")
        return value


def to_json_dict(raw: dict) -> dict:
    return User.parse_obj(raw).dict()
