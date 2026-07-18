# Contributing

Thanks for your interest in PDF Editor! Contributions — bug reports, fixes,
features, docs — are welcome.

## Ground rules

The most important rule is the **layering boundary**:

- `src/pdfcore/` is a headless engine (pure Python + PyMuPDF). **It must never
  import Qt / PySide6.** A guard test enforces this.
- `src/pdfapp/` is the PySide6 UI. It may import `pdfcore`, never the other way.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design.

## Development setup (Windows, PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# Optional, for OCR features/tests:
winget install -e --id UB-Mannheim.TesseractOCR
```

## Before you open a PR

```powershell
ruff check .
ruff format --check .
pytest -q
```

- **Engine changes need pytest coverage.** Page-manipulation ops need a round-trip
  test (open → operate → save → reopen → assert).
- Keep the diff focused; one logical change per PR.
- Tests that need Tesseract skip cleanly when it isn't installed — that's expected.

## Reporting bugs

Open an issue with steps to reproduce, your Windows version, and — if the app
misbehaved — the diagnostics log (Help → "Show diagnostics log", at
`%LOCALAPPDATA%\PDF Editor\diagnostics.log`).

## Licensing

By contributing you agree that your contributions are licensed under the project's
**AGPL-3.0** licence.
