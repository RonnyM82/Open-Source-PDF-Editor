"""List-marker recognition (pure classifier)."""

from __future__ import annotations

import pytest

from pdfcore.lists import (
    is_bullet_only,
    leading_marker,
    split_leading_marker,
)


@pytest.mark.parametrize(
    "text,kind,marker,ordinal",
    [
        ("• Buy milk", "bullet", "•", None),
        ("•\tBuy milk", "bullet", "•", None),
        ("1. First step", "decimal", "1.", 1),
        ("2) Second step", "decimal", "2)", 2),
        ("10. Tenth", "decimal", "10.", 10),
        ("a. Alpha", "alpha", "a.", 1),
        ("c) Gamma", "alpha", "c)", 3),
        ("(iv) Roman four", "roman", "(iv)", 4),
        ("iii. Roman three", "roman", "iii.", 3),
        ("  1. leading space", "decimal", "1.", 1),
    ],
)
def test_leading_marker_recognised(text, kind, marker, ordinal):
    m = leading_marker(text)
    assert m is not None
    assert m.kind == kind
    assert m.text == marker
    assert m.ordinal == ordinal
    # end points at the body start
    assert text[m.end :].startswith(
        ("Buy", "First", "Second", "Tenth", "Alpha", "Gamma", "Roman", "leading")
    )


@pytest.mark.parametrize(
    "text",
    [
        "Hello world",  # plain prose
        "3.14 is pi",  # a decimal, not a marker (no space after the dot)
        "1.text",  # no gutter after the marker
        "",  # empty
        "The 1. item",  # marker not at the start
        "$1,410.47",  # money
    ],
)
def test_leading_marker_rejected(text):
    assert leading_marker(text) is None


def test_is_bullet_only():
    assert is_bullet_only("•")
    assert is_bullet_only(" • ")
    assert not is_bullet_only("• text")
    assert not is_bullet_only("-")  # a lone hyphen is never a bullet
    assert not is_bullet_only("1.")


def test_split_leading_marker_roundtrips():
    marker, body = split_leading_marker("1.  First step")
    assert marker == "1.  "
    assert body == "First step"
    assert marker + body == "1.  First step"

    marker, body = split_leading_marker("• Buy milk")
    assert marker == "• "
    assert body == "Buy milk"

    assert split_leading_marker("plain text") is None


def test_lists_module_is_qt_free():
    """The engine boundary: pdfcore.lists must not import Qt (like all of
    pdfcore)."""
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent(
        """
        import sys
        import pdfcore.lists  # noqa: F401
        assert "PySide6" not in sys.modules
        for name in list(sys.modules):
            assert not name.lower().startswith("pyside"), name
        print("ok")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"),
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
