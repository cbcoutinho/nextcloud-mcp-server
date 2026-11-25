"""BM25_LANGUAGES parsing and the per-script stemmer (GH #1918).

The real FastEmbed model is not loaded here (that needs a HuggingFace
download); tests/integration/test_bm25_languages_qdrant_server.py covers the
encode -> Qdrant -> query round trip.
"""

import copy
import pickle

import pytest
from fastembed.sparse.bm25 import supported_languages
from hypothesis import given
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


@pytest.mark.parametrize("raw", ["", " , ", None])
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


_ONE_PER_SCRIPT = st.dictionaries(
    st.sampled_from(sorted(set(BM25_LANGUAGE_SCRIPTS.values()))),
    st.just(None),
    min_size=1,
).flatmap(
    lambda scripts: st.tuples(
        *[
            st.sampled_from(
                [lang for lang, s in BM25_LANGUAGE_SCRIPTS.items() if s == script]
            )
            for script in scripts
        ]
    )
)


@given(languages=_ONE_PER_SCRIPT, data=st.data())
def test_parse_is_order_insensitive(languages, data):
    """Any ordering of a valid set parses to the same sorted tuple, so the
    collection name does not depend on how the operator wrote the list."""
    shuffled = data.draw(st.permutations(languages))
    assert parse_bm25_languages(",".join(shuffled)) == tuple(sorted(languages))


@pytest.mark.parametrize(
    ("word", "script"),
    [
        ("контекста", "CYRILLIC"),
        ("running", "LATIN"),
        ("straße", "LATIN"),
        ("λόγος", "GREEK"),
        ("2026x", "LATIN"),
        ("file_id", "LATIN"),
        ("2026", None),
        ("__", None),
    ],
)
def test_script_of(word, script):
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
