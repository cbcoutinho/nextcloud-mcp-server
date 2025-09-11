from typing import List, Optional
from pydantic import BaseModel, Field


class User(BaseModel):
    """Model for creating a new user."""

    userid: str
    password: Optional[str] = None
    displayName: Optional[str] = None
    email: Optional[str] = None
    groups: Optional[List[str]] = Field(default_factory=list)
    subadmin: Optional[List[str]] = Field(default_factory=list)
    quota: Optional[str] = None
    language: Optional[str] = None


class UserDetails(BaseModel):
    """Model for retrieving detailed user information."""

    enabled: bool
    id: str
    quota: str
    email: str
    displayname: str = Field(
        alias="display-name"
    )  # Handle both displayname and display-name
    phone: Optional[str] = None
    address: Optional[str] = None
    website: Optional[str] = None
    twitter: Optional[str] = None
    groups: Optional[List[str]] = Field(default_factory=list)


class Group(BaseModel):
    """Model for a user group."""

    id: str
