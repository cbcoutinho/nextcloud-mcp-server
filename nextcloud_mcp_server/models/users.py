from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, ConfigDict, Field


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

    model_config = ConfigDict(populate_by_name=True)

    enabled: bool
    id: str
    quota: Union[str, Dict[str, Any]]  # Can be string or quota object
    email: Optional[str] = None  # Can be null
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
