# Update notification and in-place upgrade

Status: **BUILT, suite green** (2026-08-26), on branch `feat/updates`. UP1
through UP4 are committed; UP5 is this documentation plus Scott's hands-on
pass, whose checklist is section 7. Scott approved the feature shape: the app
checks GitHub for a newer release, tells the user with a banner, and can
upgrade itself in place through the silent installer. The deferral options are
his spec verbatim: skip a version entirely (quiet until the version after it
ships), or skip for 7 days and be reminded again. The About dialog gains a
"check for updates" button and an update status line.

Two things changed from the plan as written, both recorded in place below. The
Inno relaunch mechanism in section 3.5 was probed before UP4 was built and
works exactly as hoped, so it is no longer an open question. Section 6's
checksum question survives untouched.

Everything else was built as planned. Three defects were caught by the work's
own tests rather than by a user, and each is worth remembering because each
was a case of correct-looking code failing in the WRONG direction: an
unreadable skipped-version value silenced every future release instead of
being ignored; opening the About dialog stacked a checker connection per open,
each holding a closed dialog; and a result arriving after the dialog closed
reached a deleted C++ object.

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
   running the setup by hand sees exactly today's behaviour.

**The relaunch mechanism, probed and confirmed (2026-08-26).** The `[Run]`
entry carries `Check: ShouldRelaunch`, and that function returns
`ExpandConstant('{param:RELAUNCH|0}') = '1'`. A throwaway installer built
against the installed Inno Setup 6 proved both directions: run with
`/SILENT /NORESTART` the gated entry did NOT fire, and with
`/SILENT /NORESTART /RELAUNCH=1` it did. The load-bearing detail is that the
entry must NOT carry the usual `postinstall skipifsilent` flags, since those
are precisely what suppresses a `[Run]` entry during a silent install. The
real `installer/pdf-editor.iss` was then compiled end to end to confirm the
new `[Code]` section is valid in place.

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

- **UP1 — the logic, no UI. DONE.** `pdfapp/updates.py`: tag parsing and
  version compare, release-JSON to `UpdateInfo`, the should-notify decision,
  the install-kind read (composing `sys.frozen` with
  `portable.is_portable()`), and the settings keys. 74 tests cover the whole
  decision table in 3.2, the malformed-tag and `"0.0.0"` guards, and a
  payload pinned from the real API.
- **UP2 — check and banner. DONE.** The background fetch bridge, the
  automatic launch check with its 24 h throttle and kill switch, the banner
  widget with its three buttons and cross, `theme.update_banner_qss()`, and
  the diagnostics breadcrumb.
- **UP3 — the About dialog section. DONE.** Status label, Check for updates
  button, the Update now / Open download page action per build kind, and the
  `update_status_text` helper with wording tests.
- **UP4 — the upgrade itself. DONE.** The download with progress and size
  check, the close-then-launch ordering from 3.5, the portable
  download-page branch, and the installer's gated relaunch entry.
- **UP5 — docs and the hands-on pass. Docs DONE, pass OUTSTANDING.**
  CLAUDE.md section, this document, and the README's "Staying up to date"
  section (what the app sends, and how to switch the check off). The
  hands-on checklist is section 7.

## 5. Testing rules that hold across all milestones

The suite never performs a network request: the fetch seam is monkeypatched
everywhere, and the default feed URL is only ever contacted by a running
app. Dialog-shaped code follows the existing convention of `isVisible()`
gates with tests driving the dispatch methods directly. Anything that reads
the clock takes it as a parameter so the snooze arithmetic is testable on
fixed dates.

## 6. Defects the build's own tests caught

Recorded because all three were correct-looking code failing in the wrong
direction, which is the kind of thing that survives a casual reread.

**An unreadable skipped-version value silenced every future release.** The
skip rule was written as "stay quiet unless the offered release is newer than
the skipped one", and `is_newer` returns False for an unparseable input. So a
corrupt `update_skipped_version` made every future release read as
not-newer-than-the-skip, and the banner would never have appeared again. The
fix is to apply the rule only when the stored value actually parses, which is
what section 3.2's fail-open promise always meant.

**Opening the About dialog stacked a signal connection each time.** The first
cut connected the checker's `finished` and `failed` signals to a lambda
holding that particular dialog, so every reopen added a connection that
outlived its dialog. MainWindow now keeps one `_about_dialog` reference that
the existing result handlers refresh.

**A result arriving after the dialog closed reached a deleted C++ object.**
The checker deliberately outlives the dialog, so this is reachable in normal
use: open About, press Check, close the box before the answer arrives.
`_refresh_about` swallows exactly that `RuntimeError`.

## 7. The hands-on pass (outstanding)

Everything below needs two real frozen builds, because the parts that cannot
be tested from source are exactly the parts that matter: whether the running
app can be replaced while it is closing, and whether Windows lets a silent
install through without interrupting it.

**Setting it up.** Build the current version, then bump `version` in
`pyproject.toml` to something clearly higher, build again, and keep both
installers. Write a small JSON file shaped like the API response naming the
higher version, with its `browser_download_url` set to a `file:///` URL
pointing at the newer installer on disk, and its `size` set to that file's
exact byte count. Install the LOWER version, then launch it with
`PDF_EDITOR_UPDATE_FEED` set to that file. No throwaway GitHub release is
needed, and nothing touches the network.

Then walk these, in order:

1. Launch and wait a few seconds. The banner appears naming the higher
   version.
2. Press **Skip this version**. Close the app, launch again, and confirm the
   banner stays away. Then edit the feed to name a version higher still and
   confirm the banner comes back on the next launch. That is the skip
   expiring on its own, which is the half of the spec most easily got wrong.
3. Reset the skip (delete `update_skipped_version` from `settings.json` in
   `%LOCALAPPDATA%\PDF Editor`), press **Remind me in 7 days**, relaunch, and
   confirm silence. Wind the machine clock forward eight days, relaunch, and
   confirm the banner returns.
4. Open **Help → About PDF Editor** while a skip is active. The status line
   must still report the available version, and **Check for updates** must
   work. This is the "a person who asked deserves the truth" rule.
5. With an unsaved edit open in a tab, press **Update now** and cancel at the
   unsaved-changes prompt. Nothing should install, the app should stay open,
   and the work should still be there.
6. Press **Update now** again and let it run. Watch for: the download
   progress dialog and its Cancel button; the app closing; the installer's
   progress bar appearing; the app starting again by itself; and About then
   reporting the new version. Note whether SmartScreen interrupts the silent
   install — it should not, because the file was downloaded by the app rather
   than by a browser, but that is reasoning, not something we have observed.
7. Repeat step 1 on the **portable** ZIP build. The banner must appear with
   **Open download page** rather than Update now, and pressing it must open
   the browser and install nothing.
8. Cancel a download midway and confirm no `.part` file survives in
   `%TEMP%\PDF Editor Updates`, then start it again and let it finish.

## 8. Open questions

- Checksums. Publishing a SHA-256 beside each release asset would let the
  updater verify the download cryptographically instead of by size. It
  means a release-process change, so it is deferred, recorded here rather
  than quietly dropped.
- Whether the About dialog should also show when the last automatic check
  ran. Left out for now; the status is honest without it and the dialog
  stays uncluttered.
