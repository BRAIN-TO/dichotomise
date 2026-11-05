"""Filename-safe text."""

from __future__ import annotations

import pytest

from dichotomise.utils.text import safe_filename_text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("DWI 64 dir", "DWI_64_dir"),
        ("bto_CS4.8_MP2RAGE_0.8mm_iso", "bto_CS4.8_MP2RAGE_0.8mm_iso"),
        ("a/b\\c:d", "a_b_c_d"),
        ("", "NA"),
        ("___", "NA"),
    ],
)
def test_safe_filename_text(value: str, expected: str) -> None:
    assert safe_filename_text(value) == expected
