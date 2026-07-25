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
does not send documents anywhere. Relevant classes of issue:

- **Malformed PDFs** causing crashes or unexpected behaviour when opened. Most PDF
  parsing is handled by the bundled **PyMuPDF / MuPDF** engine; genuine PDF-parsing
  vulnerabilities may belong upstream, but report them here and we'll help route them.
- **Digital signatures** (pyHanko-based): anything that makes a *tampered* signed
  document verify or display as intact, lets the signature status surface be
  spoofed, or leaks key material. Certificates/passwords are prompted per use and
  never stored; the signature library stores image assets and certificate *paths*
  only.
- **Password protection** (AES-256): anything that writes a protected document
  without its encryption when it should be kept, leaks a password (they are held
  in memory only, never persisted), or lets *this app* bypass permission
  restrictions it claims to honour. Note the design boundary: PDF permission
  flags (the owner-password restrictions) are an honor system that binds
  compliant readers — third-party tools ignoring them is a limitation of the PDF
  standard, not a vulnerability in this app. The open password is the only
  cryptographic protection.
