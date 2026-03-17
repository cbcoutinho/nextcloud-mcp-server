"""XML utility functions for safe content handling."""

from xml.sax.saxutils import escape as _xml_escape


def escape_xml(value: str) -> str:
    """Escape XML special characters in user-provided values.

    Prevents XML injection when interpolating values into XML documents.
    Escapes &, <, >, ', and " characters.
    """
    return _xml_escape(value, entities={"'": "&apos;", '"': "&quot;"})
