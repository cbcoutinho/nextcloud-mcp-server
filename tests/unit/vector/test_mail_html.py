"""Unit tests for the mail HTML -> Markdown conversion (Deck card #1618).

The output is indexed text *and* what Astrolabe's chunk viewer renders through
markdown-it (``html: false``, ``breaks: true``). The fixtures are synthetic and
shaped like the Mail app's sanitized HTML: newsletter layout tables, Gmail and
Outlook quoted replies, data tables, inline images.
"""

import re

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from nextcloud_mcp_server.vector.html_processor import (
    escape_setext_underlines,
    html_to_markdown,
    mail_html_to_markdown,
)

pytestmark = pytest.mark.unit

NEWSLETTER = """
<html><head><title>Digest</title><style>.x{color:red}</style></head><body>
<table width="100%" cellpadding="0"><tr><td align="center">
  <table width="600"><tr><td>
    <h1>Weekly Digest</h1>
    <p>Hello <b>reader</b>, here is <i>this week</i>:</p>
    <ul><li><a href="https://example.com/a">Article A</a></li><li>Item two</li></ul>
    <img src="https://example.com/banner.png" alt="Spring sale banner">
    <img src="https://example.com/pixel.gif" width="1" height="1">
    <p>Unsubscribe <a href="https://example.com/u">here</a></p>
  </td></tr></table>
</td></tr></table>
</body></html>
"""


def test_newsletter_layout_tables_are_flattened():
    out = mail_html_to_markdown(NEWSLETTER)
    assert out == (
        "# Weekly Digest\n\n"
        "Hello **reader**, here is *this week*:\n\n"
        "- [Article A](https://example.com/a)\n"
        "- Item two\n\n"
        "[image: Spring sale banner]\n\n"
        "Unsubscribe [here](https://example.com/u)"
    )


def test_newsletter_regression_baseline_squashes_into_one_cell():
    """Pins the loss this converter exists to fix, via the generic converter."""
    out = html_to_markdown(NEWSLETTER)
    assert "# Weekly Digest" not in out
    assert "| --- |" in out


def test_gmail_quoted_reply_stays_a_nested_blockquote():
    html = (
        "<div>Sounds good.<br></div>"
        '<div class="gmail_quote"><div class="gmail_attr">On Mon, Alice wrote:<br></div>'
        '<blockquote class="gmail_quote"><div>Shall we meet?<br>Line two.</div>'
        '<blockquote type="cite"><div>Older quote</div></blockquote></blockquote></div>'
    )
    assert mail_html_to_markdown(html) == (
        "Sounds good.\n\n"
        "On Mon, Alice wrote:\n\n"
        "> Shall we meet?  \n"
        "> Line two.\n"
        ">\n"
        "> > Older quote"
    )


def test_data_table_escapes_pipes_in_cells():
    html = (
        "<table><thead><tr><th>Name</th><th>Score</th></tr></thead>"
        "<tbody><tr><td>Carol | Jr</td><td>7</td></tr></tbody></table>"
    )
    assert mail_html_to_markdown(html) == (
        "| Name | Score |\n| --- | --- |\n| Carol \\| Jr | 7 |"
    )


def test_headerless_data_table_uses_first_row_as_header():
    html = "<table><tr><td>Q1</td><td>100</td></tr><tr><td>Q2</td><td>200</td></tr></table>"
    assert mail_html_to_markdown(html) == ("| Q1 | 100 |\n| --- | --- |\n| Q2 | 200 |")


def test_data_table_inside_layout_table_survives():
    html = (
        "<table><tr><td><p>Intro</p>"
        "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
        "</td></tr></table>"
    )
    assert mail_html_to_markdown(html) == (
        "Intro\n\n| A | B |\n| --- | --- |\n| 1 | 2 |"
    )


@pytest.mark.parametrize(
    "table",
    [
        pytest.param(
            '<table role="presentation"><tr><td>a</td><td>b</td></tr></table>',
            id="role-presentation",
        ),
        pytest.param(
            "<table><tr><td>only</td></tr><tr><td>column</td></tr></table>",
            id="single-column",
        ),
    ],
)
def test_layout_tables_by_declaration_or_shape(table):
    assert "|" not in mail_html_to_markdown(table)


def test_br_lines_and_inline_formatting():
    html = (
        "<div>Kind regards,<br>Bob<br>Acme Ltd</div>"
        '<p><strong>bold</strong> <em>italic</em> <a href="https://example.com">link</a></p>'
    )
    assert mail_html_to_markdown(html) == (
        "Kind regards,  \nBob  \nAcme Ltd\n\n"
        "**bold** *italic* [link](https://example.com)"
    )


def test_images_never_become_markdown_images():
    html = (
        '<p><img src="cid:logo123" alt="Company [logo]"></p>'
        '<a href="https://example.com"><img src="https://example.com/b.png" alt="Shop"></a>'
    )
    out = mail_html_to_markdown(html)
    assert "![" not in out
    assert "cid:" not in out
    assert "b.png" not in out
    assert "[image: Company (logo)]" in out
    assert "[[image: Shop]](https://example.com)" in out


def test_head_and_style_content_is_dropped():
    html = "<head><title>T</title><style>p{color:red}</style></head><p>Body</p>"
    assert mail_html_to_markdown(html) == "Body"


def test_empty_input():
    assert mail_html_to_markdown(None) == ""
    assert mail_html_to_markdown("") == ""


# --- properties -------------------------------------------------------------

_cell_text = st.text(
    alphabet=st.characters(
        codec="utf-8", exclude_categories=("Cs", "Cc"), exclude_characters="<>&\\"
    ),
    min_size=1,
    max_size=12,
).filter(lambda s: s.strip())


@st.composite
def data_tables(draw):
    """A header row plus body rows of a fixed width, built with its width."""
    cols = draw(st.integers(min_value=2, max_value=4))
    rows = draw(
        st.lists(
            st.lists(_cell_text, min_size=cols, max_size=cols),
            min_size=1,
            max_size=4,
        )
    )
    head = "".join(f"<th>{c}</th>" for c in rows[0])
    body = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows[1:]
    )
    return f"<table><tr>{head}</tr>{body}</table>", cols, len(rows)


@given(data_tables())
@example(("<table><tr><th>a|b</th><th>c</th></tr></table>", 2, 1))
def test_data_table_rows_keep_their_column_count(case):
    """Every row has exactly ``cols`` cells, whatever the cell text holds."""
    html, cols, nrows = case
    lines = mail_html_to_markdown(html).splitlines()
    table_lines = [line for line in lines if line.startswith("|")]
    # header + separator + body rows
    assert len(table_lines) == nrows + 1
    for line in table_lines:
        unescaped_pipes = len(re.findall(r"(?<!\\)\|", line))
        assert unescaped_pipes == cols + 1


@given(
    alt=st.text(max_size=30),
    src=st.sampled_from(
        ["https://tracker.example/p.gif", "cid:part1.abc", "data:image/png;base64,AA"]
    ),
)
def test_images_are_text_placeholders_only(alt, src):
    html = f'<p>x</p><img src="{src}" alt="{alt.replace(chr(34), "")}">'
    out = mail_html_to_markdown(html)
    assert src not in out
    assert "![" not in out


@given(st.text())
@example("<table><tr><td><table>")
@example("<img alt='[a](javascript:x)'>")
def test_mail_html_to_markdown_never_raises(html):
    assert isinstance(mail_html_to_markdown(html), str)


_setext_ish = st.lists(
    st.one_of(
        st.text(alphabet="-= \t", max_size=6),
        st.text(
            alphabet=st.characters(codec="ascii", exclude_characters="\n\r"),
            max_size=10,
        ),
    ),
    max_size=8,
).map("\n".join)


@given(_setext_ish)
@example("Thanks\n-- \nAlice")
@example("Title\n=====")
def test_escape_setext_underlines_only_inserts_backslashes(text):
    """Escaping is lossless, idempotent, and leaves no setext underline line."""
    escaped = escape_setext_underlines(text)
    assert escape_setext_underlines(escaped) == escaped
    # Removing one backslash per escaped line restores the input.
    restored = re.sub(r"^( {0,3})\\([-=])", r"\1\2", escaped, flags=re.MULTILINE)
    assert restored == text
    assert not re.search(r"^ {0,3}[-=]+[ \t]*$", escaped, flags=re.MULTILINE)
