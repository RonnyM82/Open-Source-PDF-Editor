# Update notification and in-place upgrade

Status: **PLANNED** (2026-08-26). Nothing is built yet. Scott has approved the
feature shape: the app checks GitHub for a newer release, tells the user with
a banner, and can upgrade itself in place through the silent installer. The
deferral options are his spec verbatim: skip a version entirely (quiet until
the version after it ships), or skip for 7 days and be reminded again. The
About dialog gains a "check for updates" button and an update status line.

## 1. What this adds

Four user-visible things, and nothing else changes:

1. On launch, the app quietly asks GitHub whether a newer release exists.
   Offline or failed checks stay silent. A machine that never sees the
   internet never notices the feature exists.
2. When a newer version is available, a banner appears across the top of the
   main window: "Version 0.12.0 is available", with buttons **Update now**,
   **Remind me in 7 days**, and **Skip this version**, plus a close cross
   that dismisses it for this session only.
3. **Update now** downloads the release's setup program, closes the app
   (asking about unsaved changes exactly as closing always does), runs the
   installer silently, and relaunches the app on the new version.
4. Help → About gains an Updates section: a status line ("You're on the
   latest version" / "Version 0.12.0 is available" / "Couldn't reach
   github.com") and a **Check for updates** button that runs a fresh check on
   the spot. The manual check always reports the truth, ignoring any skip or
   snooze the user has set.

## 2. Facts this plan stands on

Verified against the live repo and codebase on 2026-08-26:

- Releases are tagged `v0.11.0` (v prefix plus the pyproject version) and
  carry exactly two assets with predictable names:
  `pdf-editor-setup-<version>.exe` and `pdf-editor-portable-<version>.zip`.
  Checked with `gh release view` today. The updater needs no release-process
  change at all; the existing `build.ps1 -Installer` + `gh release create`
  routine already produces everything it consumes.
- GitHub's public API endpoint `/repos/<owner>/<repo>/releases/latest`
  returns the tag name, the asset list (each with name, download URL, and
  byte size), and the release page URL. It excludes drafts and prereleases.
  Unauthenticated callers get 60 requests per hour per address, which is
  plenty for a once-a-day check. The response shape gets pinned by a fixture
  in UP1 and probed once for real at the start of UP2.
- `pdfcore/version.py` `app_version()` is the single version source and
  falls back to the loudly-wrong `"0.0.0"` when nothing is readable. The
  updater must treat `"0.0.0"` as "version unknown" and stay quiet, or a
  broken bundle would nag about an upgrade forever.
- The app already knows which kind of build it is. `portable.is_portable()`
  is true for a frozen build with the marker file beside the exe (the ZIP);
  frozen without the marker is the installed build; not frozen is a dev run.
  No registry sniffing is needed.
- `pdfapp/settings.py` is the flat JSON store, Qt-free, persisted
  immediately, shared safely between windows. The three new keys in
  section 3.6 ride it unchanged.
- The installer (`installer/pdf-editor.iss`) already has everything an
  in-place upgrade needs: the stable AppId upgrades the existing install
  rather than adding a second copy, `CloseApplications=yes` shuts a running
  exe via Restart Manager, the install is per-user so no admin prompt
  appears, and Inno's standard `/SILENT /NORESTART` flags run it with just a
  progress bar. The one gap is relaunching the app afterwards: the existing
  `[Run]` entry is `postinstall skipifsilent`, so a silent install currently
  ends with nothing on screen. UP4 adds a second `[Run]` entry gated on a
  custom `/RELAUNCH=1` parameter.
- The About dialog (`pdfapp/about_dialog.py`) is our own QDialog with a pure
  `about_html` helper that offscreen tests already assert against. Adding a
  live status row and a button follows its existing conventions.
- No new dependency is needed. The check and the download use stdlib
  `urllib` on a background thread; rule 4's pyproject/README/NOTICE ritual
  never triggers.
- A file downloaded by the app itself (rather than by a browser) carries no
  mark-of-the-web, so SmartScreen does not interrupt the silent install.
  This is expected behaviour rather than something we have probed; the UP5
  hands-on pass confirms it on a real machine.

## 3. Design

### 3.1 The check

A new Qt-free module `pdfapp/updates.py` (the `settings.py` / `portable.py`
pattern) owns all the logic: parse a `vX.Y.Z` tag into a comparable tuple
(malformed tags read as "no update"), turn the release JSON into an
`UpdateInfo` (version, installer asset name/URL/size, release page URL), and
answer the one decision question, "should this launch show the banner"
(section 3.2). The actual fetch is one small function
(`fetch_latest_release(url) -> dict`) so tests replace it at a single seam
and never touch the network.

The Qt side runs that fetch on a background thread and delivers the result
to the main thread through a signal (signals emitted from a Python thread
arrive queued, which is the safe way). The automatic check fires a few
seconds after the window shows, so startup speed never depends on the
network, and runs at most once per 24 hours (`update_last_check` in
settings; the shared settings file also keeps a second window from checking
again). Any failure is logged as a diagnostics breadcrumb and shown nowhere.

### 3.2 When to notify

The banner appears only when every row of this table passes. The manual
check in the About dialog ignores the last two rows and always reports what
it found.

| Condition | Why |
| --- | --- |
| The build is frozen (installed or portable) | Dev runs check manually from About; nagging a source checkout helps nobody. |
| Our own version is known (not `"0.0.0"`) | A broken bundle must not nag forever. |
| The latest release parses and is newer than what is running | The whole point. |
| The latest release is newer than `update_skipped_version`, if set | Scott's "skip this version": quiet until the NEXT version ships, at which point the skip is superseded and the banner returns. |
| Today is on or after `update_snooze_until`, if set | Scott's "skip for 7 days": quiet for a week regardless of what ships in the meantime, then remind again. |

Skip and snooze are independent keys and neither clears the other; the
table's two rows combine naturally (a skipped 0.12.0 plus a snooze set on
0.13.0 stays quiet until both rules say show). Neither key is ever cleaned
up proactively; superseded values are harmless and the rules ignore them.

### 3.3 The banner

A new `pdfapp/update_banner.py`, modelled line-for-line on
`signature_banner.py` but living once at the top of the MainWindow (above
the tab widget) rather than per document, because an update is an app-level
fact. Styled by a new `theme.update_banner_qss()` in the addendum (accent
colour, not the signature banner's red/green). The three buttons write their
outcome and hide the banner:

- **Update now** starts the flow in section 3.5.
- **Remind me in 7 days** writes `update_snooze_until` = today + 7 days.
- **Skip this version** writes `update_skipped_version` = the offered
  version.
- The close cross writes nothing, so the banner simply returns next launch.

On a portable build the first button reads **Open download page** instead
and opens the release page in the browser (section 3.5 explains why the
portable build is not upgraded in place). No toolbar changes anywhere, so
`_STATE_VERSION` stays where it is.

### 3.4 The About dialog

The dialog gains an Updates row under the version: a status label plus a
**Check for updates** button. The label walks through "Not checked yet" →
"Checking…" → one of "You're on the latest version (0.11.0)" / "Version
0.12.0 is available" / "Couldn't reach github.com. Check your connection."
When an update is known and the build is installed, an **Update now** button
appears beside the status and enters the same flow as the banner's button;
on portable and dev builds it is an **Open download page** link instead. The
wording never claims more than the check proved.

The status is session-state on the MainWindow (the dialog is short-lived
and rebuilt each open, per its own conventions), handed in at construction.
A pure `update_status_text(status) -> str` helper joins `about_html` so the
offscreen tests assert wording without a window.

### 3.5 The in-place upgrade

The flow for an installed build, in order, because the order is the safety:

1. Download `pdf-editor-setup-<version>.exe` to the user's temp directory,
   with a cancellable progress dialog (the file is on the order of 150 MB).
   Verify the byte count matches the size the API reported; a mismatch
   fails with plain words and deletes the file. If a previous attempt
   already left a size-matching file there, reuse it instead of downloading
   again.
2. Ask the window to close. This runs the existing per-tab unsaved-changes
   prompts untouched. If the user cancels, the update stops here, nothing
   has been launched, and the downloaded file waits for next time.
3. Only after the close is accepted, start the installer detached
   (`/SILENT /NORESTART /RELAUNCH=1`) and let the process exit. `/SILENT`
   rather than `/VERYSILENT` is deliberate: the user just watched their app
   close, and the installer's progress bar is the evidence something is
   happening. `CloseApplications=yes` remains as the safety net if our exit
   races the installer's file copy.
4. The installer upgrades in place (stable AppId, same folder, stale
   `_internal` wiped as today) and relaunches the app via the new `[Run]`
   entry, which fires only when `/RELAUNCH=1` was passed, so a person
   running the setup by hand sees exactly today's behaviour. The `{param:}`
   check needs a small `[Code]` function; probing that syntax against our
   Inno version is the first task of UP4.

Downloads come over HTTPS from the one hard-coded repo, and the asset is
picked by its exact expected name. We do not publish checksums today; the
size check plus HTTPS is the honest current level, and section 6 records
the checksum question rather than pretending it is solved.

### 3.6 Portable and dev builds

The portable exe cannot replace itself while running, and its whole point
is not writing to the host machine, so it is never upgraded in place. It
still gets the check and the banner; its action button opens the release
page, and the user replaces the folder the same way they installed it. Dev
runs get no automatic check at all, just the manual one in About.

### 3.7 Settings keys and environment switches

| Key | Meaning |
| --- | --- |
| `update_last_check` | ISO timestamp of the last automatic check; throttles to one per 24 h. |
| `update_skipped_version` | Version string the user chose to skip; quiet until something newer ships. |
| `update_snooze_until` | ISO date; quiet until it passes. |

Two environment variables, both for scripts and support rather than users:
`PDF_EDITOR_NO_UPDATE_CHECK=1` disables the automatic check entirely (the
`PDF_EDITOR_NO_DIAGNOSTICS` naming pattern), and `PDF_EDITOR_UPDATE_FEED`
points the checker at an alternative URL or local file. The second one is
how the frozen build gets tested end to end without publishing a throwaway
release: a local JSON file shaped like the API response, naming a locally
built installer.

## 4. Milestones

Each milestone is one commit at green, per the house rules.

- **UP1 — the logic, no UI.** `pdfapp/updates.py`: tag parsing and version
  compare, release-JSON to `UpdateInfo`, the should-notify decision, the
  install-kind read (composing `sys.frozen` with `portable.is_portable()`),
  and the settings keys. Tests cover the whole decision table in 3.2, the
  malformed-tag and `"0.0.0"` guards, and a fixture pinned to the real API
  response shape.
- **UP2 — check and banner.** The background fetch bridge, the automatic
  launch check with its 24 h throttle and kill switch, the banner widget
  with its three buttons and cross, `theme.update_banner_qss()`, and the
  diagnostics breadcrumb. Starts with one real API call by hand to confirm
  the fixture matches reality. Offscreen tests drive a monkeypatched fetch;
  no test touches the network.
- **UP3 — the About dialog section.** Status label, Check for updates
  button, the Update now / Open download page action per build kind, and
  the `update_status_text` helper with wording tests.
- **UP4 — the upgrade itself.** The download with progress and size check,
  the close-then-launch ordering from 3.5, the portable download-page
  branch, and the installer's gated relaunch entry (probe the `{param:}`
  syntax first). Tests monkeypatch the process launch and assert the
  argument list and the ordering; the download function is tested against a
  local file served from disk.
- **UP5 — docs and the hands-on pass.** CLAUDE.md section, PLAN.md
  milestone entry, README note (what the app phones home for, which is one
  version lookup, and how to turn it off). Then the frozen checklist: build
  two versions locally, point `PDF_EDITOR_UPDATE_FEED` at a local feed
  naming the newer one, install the older, and walk launch → banner →
  Update now → silent upgrade → relaunch → About says current. Repeat the
  banner path on the portable ZIP, confirm skip and snooze survive a
  relaunch, and confirm SmartScreen stays out of the silent install.

## 5. Testing rules that hold across all milestones

The suite never performs a network request: the fetch seam is monkeypatched
everywhere, and the default feed URL is only ever contacted by a running
app. Dialog-shaped code follows the existing convention of `isVisible()`
gates with tests driving the dispatch methods directly. Anything that reads
the clock takes it as a parameter so the snooze arithmetic is testable on
fixed dates.

## 6. Open questions

- Checksums. Publishing a SHA-256 beside each release asset would let the
  updater verify the download cryptographically instead of by size. It
  means a release-process change, so it is deferred, recorded here rather
  than quietly dropped.
- Whether the About dialog should also show when the last automatic check
  ran. Left out for now; the status is honest without it and the dialog
  stays uncluttered.
- The relaunch after a silent upgrade rests on Inno's `{param:}` constant
  and a `[Code]` check function. The mechanism is documented and our Inno
  6.3+ floor covers it, but it is unprobed until UP4, which is why UP4
  starts there.
