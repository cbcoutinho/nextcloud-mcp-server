"""Contact listings must not be dominated by embedded photo bytes.

vCards commonly carry PHOTO as base64. On a 186-contact addressbook that is
~3 MB of vCard data, and the serialised response never reached the client --
while the server logged the call as successful.

The mapper keeps including photos by default, so single-contact paths such as
update_contact are unchanged; only the list and search tools opt out, and
``has_photo`` preserves the information that one exists.
"""

import pytest

from nextcloud_mcp_server.server.contacts import _raw_contact_to_model

pytestmark = pytest.mark.unit

PHOTO = "data:image/jpeg;base64," + "A" * 4000


def _raw(**contact_info) -> dict:
    return {
        "vcard_id": "abc",
        "object_path": "/remote.php/dav/addressbooks/users/u/c/abc.vcf",
        "getetag": '"etag"',
        "contact": {"fullname": "Alice", **contact_info},
    }


class TestPhotoIsOptOut:
    def test_included_by_default(self):
        model = _raw_contact_to_model(_raw(photo=PHOTO))
        assert model.photo == PHOTO
        assert model.has_photo is True

    def test_dropped_when_list_and_search_opt_out(self):
        model = _raw_contact_to_model(_raw(photo=PHOTO), include_photo=False)
        assert model.photo is None

    def test_has_photo_survives_the_opt_out(self):
        model = _raw_contact_to_model(_raw(photo=PHOTO), include_photo=False)
        assert model.has_photo is True

    def test_contact_without_photo(self):
        model = _raw_contact_to_model(_raw(), include_photo=False)
        assert model.photo is None
        assert model.has_photo is False

    def test_empty_photo_is_not_a_photo(self):
        """An empty PHOTO must not report photo="" alongside has_photo=False."""
        model = _raw_contact_to_model(_raw(photo=""), include_photo=True)
        assert model.photo is None
        assert model.has_photo is False

    def test_other_fields_are_unaffected(self):
        model = _raw_contact_to_model(_raw(photo=PHOTO, org="ACME"))
        assert model.fn == "Alice"
        assert model.organization == "ACME"
