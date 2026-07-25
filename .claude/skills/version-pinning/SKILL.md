---
name: version-pinning
description: >-
  Decide how to declare and pin dependency versions in any code project, based on
  semantic-versioning conventions and the application-vs-library distinction. Use
  this whenever adding a dependency, writing or editing a manifest (pyproject.toml,
  requirements.txt, package.json, Cargo.toml, go.mod), setting up a lockfile,
  reviewing a dependabot/renovate PR, or deciding whether a version bump is safe.
  Reach for it even when the user only says something like "add X", "pin this",
  "should I upgrade?", or "why did the build break after an update" — version
  strategy is the real question underneath all of those.
---

# Version Pinning

Getting version constraints right is the difference between a build that is
reproducible today and still reproducible in three years, versus one that breaks
silently the next time someone runs `install`. The rules below are mechanical once
you have made two decisions. Make those two decisions first, then apply the syntax.

## Decision 1 — Is this an application or a library?

This decides everything else. Ask it before touching a manifest.

- **Application** (something you deploy or ship to end users: a CLI, a desktop app,
  a web service, a data pipeline). You control the whole environment. Goal:
  **reproducibility**. Pin tightly and commit a lockfile so every install resolves
  to the exact same bytes.
- **Library** (something other projects will depend on). You do NOT control the
  environments you land in. Goal: **compatibility**. Pin loosely with ranges so you
  don't force version conflicts on your consumers. Do NOT commit a lockfile as the
  source of truth for consumers; lockfiles are for apps and for your own CI.

If a repo is both (a library that also has a deployable app or dev environment),
keep loose ranges in the published package metadata and a lockfile for the app/CI
side. The two live side by side and do different jobs.

## Decision 2 — How mature is the dependency?

A version number is a promise about breakage. Semantic Versioning
(`MAJOR.MINOR.PATCH`) says: MAJOR = breaking change, MINOR = new features
(backwards-compatible), PATCH = fixes only. Two caveats that matter in practice:

- **`0.x` is special.** Under SemVer, anything before `1.0.0` carries no
  compatibility promise — a `0.y` MINOR bump is allowed to break the API. Treat the
  MINOR digit of a `0.x` dependency the way you'd treat MAJOR on a `1.x+` one.
- **The promise is only as good as the maintainer.** Well-run projects keep it;
  many don't. When in doubt, pin tighter and read the changelog before widening.

### How tight to pin (the dependency's own version)

| Dependency version | Pin to | Example constraint |
|---|---|---|
| `0.x` (pre-1.0) | the MINOR series | `>=0.35.1,<0.36` |
| `1.x` and above | the MAJOR series | `>=2.4,<3` |
| security-critical (crypto, TLS, auth) | never freeze forever; track upstream, widen deliberately, and lean on the lockfile for pinning | `>=42,<50` + lockfile |

The principle behind the security row: freezing a crypto or TLS library is how you
end up shipping a known CVE. Allow patch and compatible minor updates in, and let
the lockfile give you the exact reproducible pin — see the next section for why
those are different jobs.

## Constraints vs lockfiles — two separate jobs

These get conflated constantly. They are not the same thing.

- **Constraints** (in the manifest): the *range you'll accept*. `>=0.35.1,<0.36`.
  Expresses intent. Human-authored.
- **Lockfile** (resolved artifact): the *exact versions you actually got*, including
  transitive dependencies and hashes. Machine-generated. Guarantees reproducibility.

For an application you want both: loose-enough constraints so upgrades are possible,
plus a committed lockfile so every checkout is identical. For a library you want
constraints only (published), and a lockfile just for your own CI, not shipped to
consumers.

## Ecosystem syntax

Same two decisions, different spelling.

**Python** (PEP 440). Manifest is `pyproject.toml` (preferred) or `requirements.txt`.
- Range for a `0.x` dep: `"pyHanko>=0.35.1,<0.36"`
- Range for a `1.x+` dep: `"requests>=2.31,<3"`
- Compatible-release operator `~=` is shorthand: `~=2.31.0` means `>=2.31.0,<2.32`
  (locks minor), while `~=2.31` means `>=2.31,<3` (locks major). It's terse but
  easy to misread — prefer explicit `>=x,<y` in shared projects.
- Lockfiles: `uv.lock`, `poetry.lock`, or `pip-tools` (`requirements.txt` compiled
  from `requirements.in`). Commit them for applications.

**Node** (npm SemVer ranges). Manifest is `package.json`.
- Caret `^1.2.3` allows minor + patch (`>=1.2.3,<2.0.0`) — the npm default.
- Tilde `~1.2.3` allows patch only (`>=1.2.3,<1.3.0`).
- Caret behaves differently on `0.x`: `^0.35.1` resolves to `>=0.35.1,<0.36.0`,
  which is actually the correct "lock the minor" behaviour for pre-1.0 — good.
- Lockfile: `package-lock.json` (npm) / `yarn.lock` / `pnpm-lock.yaml`. Commit for
  applications.

**Rust** (`Cargo.toml`) — caret is default and implicit: `serde = "1.2"` means
`>=1.2.0,<2.0.0`. `Cargo.lock` is committed for binaries, not for libraries.

**Go** (`go.mod`) — go modules pin exact versions in `go.mod` and hashes in
`go.sum` by design; `go get -u` widens deliberately. The lockfile equivalent is
built in.

## Upgrade discipline

Pinning is not "set once and forget". It's "set a safe range, then upgrade on
purpose".

1. **Widen deliberately.** Bumping a ceiling (e.g. `<0.36` → `<0.37`) is a code
   change that goes through review, not an accident.
2. **Read the changelog before crossing a MAJOR (or a `0.x` MINOR) boundary.** That
   is exactly the boundary the version scheme is warning you about.
3. **Automate the noise, review the substance.** Renovate or Dependabot can open
   upgrade PRs for you; let them handle patch bumps automatically if CI is green,
   but keep MAJOR / `0.x`-MINOR bumps as human-reviewed PRs.
4. **Let CI + lockfile catch regressions.** A committed lockfile plus a real test
   suite is what makes an upgrade PR trustworthy.

## Worked example

Adding `pyHanko` (a `0.x` PDF-signing library, currently `0.35.1`) to a deployable
desktop application:

- Application, not library → reproducibility wins → commit a lockfile.
- Dependency is `0.x` → pin the MINOR series → `>=0.35.1,<0.36`.
- It sits on the security-critical `cryptography` library underneath, but *that* is
  a transitive dependency of pyHanko — let pyHanko declare its own range for it, and
  let your lockfile pin the resolved version. You don't re-declare it yourself
  unless you depend on it directly.
- Result in `pyproject.toml`: `"pyHanko[image-support]>=0.35.1,<0.36"`, plus a
  committed lockfile. When a `0.36` lands, bump the ceiling in a reviewed PR after
  reading its release notes.

## Quick checklist

- [ ] App or library? (App → lockfile. Library → ranges, no shipped lockfile.)
- [ ] Dependency `0.x`? → pin MINOR. `1.x+`? → pin MAJOR.
- [ ] Security-critical? → don't freeze; range + lockfile.
- [ ] Constraints in the manifest, exact pins in the lockfile — not the same file.
- [ ] Ceiling bumps are reviewed changes, gated on changelog + green CI.
