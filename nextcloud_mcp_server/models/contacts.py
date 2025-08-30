"""Pydantic models for Contacts app responses."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .base import BaseResponse, StatusResponse


class AddressBook(BaseModel):
    """Model for a Nextcloud address book."""

    uri: str = Field(description="Address book URI")
    displayname: str = Field(description="Address book display name")
    description: Optional[str] = Field(None, description="Address book description")
    ctag: Optional[str] = Field(
        None, description="Address book tag for synchronization"
    )


class ContactField(BaseModel):
    """Model for a contact field (email, phone, etc.)."""

    type: str = Field(description="Field type (e.g., 'email', 'phone', 'address')")
    value: str = Field(description="Field value")
    label: Optional[str] = Field(None, description="Field label (e.g., 'work', 'home')")
    preferred: bool = Field(
        default=False, description="Whether this is the preferred field of this type"
    )


class Contact(BaseModel):
    """Model for a Nextcloud contact."""

    uid: str = Field(description="Contact UID")
    fn: str = Field(description="Full name (formatted name)")
    given_name: Optional[str] = Field(None, description="Given name")
    family_name: Optional[str] = Field(None, description="Family name")
    organization: Optional[str] = Field(None, description="Organization")
    title: Optional[str] = Field(None, description="Job title")
    emails: List[ContactField] = Field(
        default_factory=list, description="Email addresses"
    )
    phones: List[ContactField] = Field(
        default_factory=list, description="Phone numbers"
    )
    addresses: List[ContactField] = Field(default_factory=list, description="Addresses")
    urls: List[ContactField] = Field(default_factory=list, description="URLs")
    note: Optional[str] = Field(None, description="Notes")
    photo: Optional[str] = Field(None, description="Photo URL or base64 data")
    birthday: Optional[str] = Field(None, description="Birthday (ISO date format)")
    categories: List[str] = Field(
        default_factory=list, description="Contact categories"
    )
    custom_fields: Dict[str, Any] = Field(
        default_factory=dict, description="Custom fields"
    )
    etag: Optional[str] = Field(None, description="ETag for versioning")

    @property
    def primary_email(self) -> Optional[str]:
        """Get the primary email address."""
        if not self.emails:
            return None
        # Return preferred email if available, otherwise first email
        preferred = next(
            (email.value for email in self.emails if email.preferred), None
        )
        return preferred or self.emails[0].value

    @property
    def primary_phone(self) -> Optional[str]:
        """Get the primary phone number."""
        if not self.phones:
            return None
        # Return preferred phone if available, otherwise first phone
        preferred = next(
            (phone.value for phone in self.phones if phone.preferred), None
        )
        return preferred or self.phones[0].value


class ListAddressBooksResponse(BaseResponse):
    """Response model for listing address books."""

    addressbooks: List[AddressBook] = Field(
        description="List of available address books"
    )
    total_count: int = Field(description="Total number of address books")


class ListContactsResponse(BaseResponse):
    """Response model for listing contacts."""

    contacts: List[Contact] = Field(description="List of contacts")
    addressbook: str = Field(description="Address book name")
    total_count: int = Field(description="Total number of contacts")


class CreateContactResponse(BaseResponse):
    """Response model for contact creation."""

    contact: Contact = Field(description="The created contact")
    addressbook: str = Field(description="Address book the contact was created in")


class UpdateContactResponse(BaseResponse):
    """Response model for contact updates."""

    contact: Contact = Field(description="The updated contact")
    addressbook: str = Field(description="Address book the contact belongs to")


class DeleteContactResponse(StatusResponse):
    """Response model for contact deletion."""

    deleted_uid: str = Field(description="UID of the deleted contact")
    addressbook: str = Field(description="Address book the contact was deleted from")


class CreateAddressBookResponse(BaseResponse):
    """Response model for address book creation."""

    addressbook: AddressBook = Field(description="The created address book")


class DeleteAddressBookResponse(StatusResponse):
    """Response model for address book deletion."""

    deleted_name: str = Field(description="Name of the deleted address book")
