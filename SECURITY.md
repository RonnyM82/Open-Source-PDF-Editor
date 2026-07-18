# Security Policy

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue for a
vulnerability.

Use GitHub's private reporting: go to the
[**Security → Report a vulnerability**](../../security/advisories/new) page for this
repository and file a private advisory. Include steps to reproduce and the impact
you observed. You'll get a response as soon as reasonably possible.

## Scope

PDF Editor is an offline Windows desktop application — it does not run a server and
does not send documents anywhere. The most relevant class of issue is malformed
PDFs causing crashes or unexpected behaviour when opened. Most PDF parsing is
handled by the bundled **PyMuPDF / MuPDF** engine; genuine PDF-parsing
vulnerabilities may belong upstream, but report them here and we'll help route them.
