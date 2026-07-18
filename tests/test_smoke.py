"""M0 smoke test: the engine package imports and the repo is wired up."""

import pdfcore


def test_pdfcore_imports():
    assert pdfcore.__version__
