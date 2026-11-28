"""BM25_LANGUAGES parsing and the per-script stemmer (GH #1918).

The real FastEmbed model is not loaded here (that needs a HuggingFace
download); tests/integration/test_bm25_languages_qdrant_server.py covers the
encode -> Qdrant -> query round trip.
"""

import copy
import pickle

import pytest
from fastembed.sparse.bm25 import supported_languages
from hypothesis import event, example, given
from hypothesis import strategies as st

from nextcloud_mcp_server.config import (
    BM25_LANGUAGE_SCRIPTS,
    Settings,
    parse_bm25_languages,
)
from nextcloud_mcp_server.providers.bm25 import ScriptStemmer, _script_of

pytestmark = pytest.mark.unit


def test_language_table_matches_fastembed():
    """The config-side table must accept exactly what FastEmbed can stem."""
    assert set(BM25_LANGUAGE_SCRIPTS) == set(supported_languages)


def test_default_is_english():
    assert Settings().bm25_language_list == ("english",)


def test_parse_normalises_case_order_whitespace_and_duplicates():
    assert parse_bm25_languages(" Russian, english ,RUSSIAN") == (
        "english",
        "russian",
    )


@given(st.none() | st.text(" ,\t", max_size=6))
def test_parse_rejects_empty(raw):
    with pytest.raises(ValueError, match="at least one language"):
        parse_bm25_languages(raw)


def test_parse_rejects_unsupported_language():
    with pytest.raises(ValueError, match="unsupported language.*klingon"):
        parse_bm25_languages("english,klingon")


def test_parse_rejects_two_languages_in_one_script():
    with pytest.raises(ValueError, match=r"english, german \(latin\)"):
        parse_bm25_languages("german,english,russian")


def test_invalid_setting_fails_at_startup():
    with pytest.raises(ValueError, match="BM25_LANGUAGES"):
        Settings(bm25_languages="german,english")


_SCRIPTS = sorted(set(BM25_LANGUAGE_SCRIPTS.values()))


@st.composite
def one_per_script_in_any_order(draw) -> tuple[list[str], list[str]]:
    """``(languages, shuffled)``: at most one language per script, at least one
    in all, and the same languages in any order.

    Every script draws a language and an in/out flag, and the order is a
    permutation of all scripts, so the number of draws never depends on how
    many are in. Hypothesis's mutations then never outgrow the example.
    """
    first = draw(st.sampled_from(_SCRIPTS))
    chosen = {}
    for script in _SCRIPTS:
        lang = draw(
            st.sampled_from(
                [lang for lang, s in BM25_LANGUAGE_SCRIPTS.items() if s == script]
            )
        )
        if draw(st.booleans()) or script == first:
            chosen[script] = lang
    order = draw(st.permutations(_SCRIPTS))
    return list(chosen.values()), [chosen[s] for s in order if s in chosen]


@given(one_per_script_in_any_order())
def test_parse_is_order_insensitive(case):
    """Any ordering of a valid set parses to the same sorted tuple, so the
    collection name does not depend on how the operator wrote the list."""
    languages, shuffled = case
    assert parse_bm25_languages(",".join(shuffled)) == tuple(sorted(languages))


#: Letters per script, named as ``unicodedata`` names their first word.
_LETTERS = {
    "LATIN": "azAZßéÅœ",
    "CYRILLIC": "абвЖя",
    "GREEK": "αλόςΩ",
    "ARABIC": "عرب",
    "HEBREW": "אבג",
    "CJK": "中文字",
    "HIRAGANA": "あい",
}
#: Digits, connectors, punctuation, spaces and symbols: never a letter.
_no_letters = st.text(
    st.characters(codec="utf-8", categories=["Nd", "Pc", "Pd", "Po", "Zs", "Sm"]),
    max_size=5,
)


@st.composite
def words(draw) -> tuple[str, str | None]:
    """A word and the script of its first letter (None: it has no letters)."""
    prefix = draw(_no_letters)
    if not draw(st.integers(0, 4)):
        event("no letter")
        return prefix, None
    script = draw(st.sampled_from(sorted(_LETTERS)))
    event(script)
    letter = draw(st.sampled_from(_LETTERS[script]))
    return prefix + letter + draw(st.text(st.characters(codec="utf-8"))), script


@given(words())
@example(("контекста", "CYRILLIC"))
@example(("straße", "LATIN"))
@example(("2026x", "LATIN"))
@example(("file_id", "LATIN"))
@example(("2026", None))
@example(("__", None))
def test_script_of(case):
    """A word is stemmed by its first letter's script, whatever comes after it."""
    word, script = case
    assert _script_of(word) == script


def test_script_stemmer_dispatches_by_script():
    stemmer = ScriptStemmer(("english", "russian"))
    assert {stemmer.stem_word(w) for w in ("контекста", "контекст", "контексту")} == {
        "контекст"
    }
    assert stemmer.stem_word("running") == "run"
    assert stemmer.stem_word("file_id") == "file_id"
    # No configured language for Greek: left as-is rather than mangled.
    assert stemmer.stem_word("λόγος") == "λόγος"


def test_script_stemmer_leaves_unconfigured_scripts_alone():
    assert ScriptStemmer(("russian",)).stem_word("running") == "running"


def test_script_stemmer_survives_pickle_and_deepcopy():
    """FastEmbed's parallel path pickles/deep-copies the stemmer; the Rust
    SnowballStemmer inside cannot be, so __reduce__ must rebuild it."""
    stemmer = ScriptStemmer(("english", "russian"))
    for clone in (pickle.loads(pickle.dumps(stemmer)), copy.deepcopy(stemmer)):
        assert clone.languages == stemmer.languages
        assert clone.stem_word("контексту") == "контекст"
