"""The guidance the coach cites: chunking, stemming, synonyms and age filters."""

from habitquest_backend.guidance import CHUNKS, search_guidance, stem, tokenize


def test_every_section_is_a_chunk_with_a_stable_reference():
    assert len(CHUNKS) > 30
    assert [c.ref for c in CHUNKS[:3]] == ["G1", "G2", "G3"]
    assert CHUNKS[0].title == "Sleep for growing kids: How much sleep children need"
    assert all(c.text and not c.text.startswith("#") for c in CHUNKS)


def test_tokens_drop_stop_words_and_share_stems():
    assert tokenize("Maya's brushing her teeth!") == ["maya", "brush", "teeth"]
    assert stem("tidying") == stem("tidy") and stem("go") == "go"


def test_a_search_finds_passages_through_synonyms():
    titles = [c.title for c in search_guidance("meltdown when the iPad goes off", 7)]
    assert titles and all(isinstance(t, str) for t in titles)
    assert search_guidance("stalling at bedtime", 8, limit=1)[0].title.lower().find("bed") >= 0


def test_a_search_with_no_match_finds_nothing():
    assert search_guidance("xylophone quantum", 8) == []


def test_a_search_respects_the_age_range():
    chunk = CHUNKS[0]
    assert search_guidance(chunk.title, chunk.max_age + 1) == []
