from __future__ import annotations

from dichotomise.pydcm.names import ADJECTIVES, SURNAMES, generate_name


def test_word_lists_have_no_duplicates() -> None:
    assert len(ADJECTIVES) == len(set(ADJECTIVES))
    assert len(SURNAMES) == len(set(SURNAMES))


def test_generate_name_joins_one_adjective_and_one_surname() -> None:
    name = generate_name()

    adjective, separator, surname = name.partition("_")
    assert separator == "_"
    assert adjective in ADJECTIVES
    assert surname in SURNAMES


def test_generate_name_uses_the_given_separator() -> None:
    name = generate_name(separator="-")

    assert "-" in name
    assert "_" not in name
