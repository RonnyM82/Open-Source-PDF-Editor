"""Update checking — version compare, the notify decision, and the feed fetch.

Qt-free by design (mirrors ``settings.py`` / ``portable.py`` / ``recent_files.py``):
imports only stdlib, so every rule below is unit-testable without a QApplication
and without a network. The Qt side (UP2) runs :func:`fetch_latest_release` on a
background thread and feeds the result into :func:`should_notify`.

The feed is GitHub's ``releases/latest`` endpoint, which excludes drafts and
prereleases. Its response shape is pinned by ``LATEST_RELEASE_PAYLOAD`` in
``tests/test_updates.py``, captured from the real API on 2026-08-26.

Two rules are load-bearing and easy to get wrong:

- **An unparseable version is never an update.** Tags we don't understand, and
  our OWN version reading as ``"0.0.0"`` (``pdfcore.version``'s loudly-wrong
  fallback for a build whose pyproject.toml is unreadable), both mean "stay
  quiet". Without that guard a broken bundle would nag about an upgrade forever.
- **Corrupt deferral state fails OPEN.** A garbage ``update_skipped_version`` or
  ``update_snooze_until`` is ignored, so the banner shows. Failing the other way
  would silence updates permanently on one bad write, which is worse than one
  unwanted banner.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from pdfapp import portable

REPO = "RonnyM82/Open-Source-PDF-Editor"
DEFAULT_FEED_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE_URL = f"https://github.com/{REPO}/releases/latest"

# Point the checker at another URL — or a local JSON file shaped like the API
# response — to exercise the whole flow against a locally built installer
# without publishing a throwaway release (see docs/update-plan.md section 3.7).
FEED_ENV = "PDF_EDITOR_UPDATE_FEED"
# Disables the automatic launch check entirely (the PDF_EDITOR_NO_DIAGNOSTICS
# naming pattern). The manual About-dialog check still works.
DISABLE_ENV = "PDF_EDITOR_NO_UPDATE_CHECK"

# pdfcore.version's fallback when no version source is readable. Treated as
# "version unknown", never as a real version to compare against.
UNKNOWN_VERSION = "0.0.0"

# Settings keys (docs/update-plan.md section 3.7).
LAST_CHECK_KEY = "update_last_check"
SKIPPED_VERSION_KEY = "update_skipped_version"
SNOOZE_UNTIL_KEY = "update_snooze_until"

SNOOZE_DAYS = 7
CHECK_INTERVAL_HOURS = 24
FETCH_TIMEOUT = 10.0

# Which kind of build is running. Only INSTALLED can upgrade itself in place:
# the portable exe can't replace itself while running (and its whole point is
# leaving the host untouched), and a dev run has no bundle to replace.
INSTALLED = "installed"
PORTABLE = "portable"
DEV = "dev"

_INSTALLER_ASSET = "pdf-editor-setup-{version}.exe"


# --- versions -----------------------------------------------------------
def parse_version(text: str | None) -> tuple[int, ...] | None:
    """``"v0.11.0"`` / ``"0.11.0"`` → ``(0, 11, 0)``; anything else → None.

    Deliberately strict: only a dotted run of digits is a version. A tag
    carrying a suffix (``v1.0.0-rc1``) or any other shape reads as None, which
    every caller treats as "no update" — the conservative direction.
    """
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    if cleaned[:1].lower() == "v":
        cleaned = cleaned[1:]
    parts = cleaned.split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def _greater(here: tuple[int, ...], there: tuple[int, ...]) -> bool:
    """``there > here``, padded to equal length so (0, 12) == (0, 12, 0)."""
    width = max(len(here), len(there))
    return there + (0,) * (width - len(there)) > here + (0,) * (width - len(here))


def is_newer(current: str | None, latest: str | None) -> bool:
    """True when ``latest`` is a strictly newer version than ``current``.

    False whenever either side is unparseable or ``current`` is the unknown
    fallback — a build that can't state its own version must never be told it
    is out of date.

    That ``UNKNOWN_VERSION`` guard is about OUR OWN version specifically, which
    is why :func:`should_notify` compares a skipped version with `_greater`
    instead of calling this: a stored skip of ``"0.0.0"`` is an ordinary
    version to compare against, and routing it through here made every future
    release read as not-newer and silenced the banner permanently.
    """
    if current == UNKNOWN_VERSION:
        return False
    here = parse_version(current)
    there = parse_version(latest)
    if here is None or there is None:
        return False
    return _greater(here, there)


# --- the release feed ---------------------------------------------------
@dataclass(frozen=True)
class UpdateInfo:
    """One release, reduced to what the updater needs.

    ``installer_url`` is None (and ``installer_size`` 0) when the release
    carries no setup asset — the banner then offers the download page instead
    of an in-place upgrade, exactly as it does for a portable build. A size of
    0 also means the feed gave no length to verify against, which
    :func:`download_installer` falls back to the server's Content-Length for.
    """

    version: str
    page_url: str
    installer_name: str | None = None
    installer_url: str | None = None
    installer_size: int = 0


def feed_url() -> str:
    """The release feed to check — the override when set, else GitHub's API."""
    return os.environ.get(FEED_ENV) or DEFAULT_FEED_URL


def release_to_info(data: Any) -> UpdateInfo | None:
    """Turn a ``releases/latest`` response into an :class:`UpdateInfo`.

    None when the payload isn't a release object with a parseable tag. The
    installer asset is matched by its exact expected name, never by position or
    by picking whichever ``.exe`` turns up.
    """
    if not isinstance(data, dict):
        return None
    version_parts = parse_version(data.get("tag_name"))
    if version_parts is None:
        return None
    version = ".".join(str(p) for p in version_parts)
    page_url = data.get("html_url") or RELEASES_PAGE_URL
    wanted = _INSTALLER_ASSET.format(version=version)
    assets = data.get("assets")
    if isinstance(assets, list):
        for asset in assets:
            if not isinstance(asset, dict) or asset.get("name") != wanted:
                continue
            url = asset.get("browser_download_url")
            if not isinstance(url, str) or not url:
                continue
            size = asset.get("size")
            return UpdateInfo(
                version=version,
                page_url=page_url,
                installer_name=wanted,
                installer_url=url,
                installer_size=size if isinstance(size, int) and size > 0 else 0,
            )
    return UpdateInfo(version=version, page_url=page_url)


def fetch_latest_release(url: str | None = None, timeout: float = FETCH_TIMEOUT) -> Any:
    """Fetch and decode the release feed. Raises on any network/parse failure.

    This is the ONE network seam: tests monkeypatch it and never touch the wire.
    A feed override that isn't an http(s) URL is read as a local file, so the
    frozen build can be walked end to end against a hand-written feed.
    """
    target = url or feed_url()
    if not target.lower().startswith(("http://", "https://")):
        return json.loads(Path(target).read_text(encoding="utf-8"))
    request = urllib.request.Request(
        target,
        headers={
            # GitHub rejects API requests with no User-Agent.
            "User-Agent": f"PDFEditor/{_current_version()}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _current_version() -> str:
    """This build's version, never raising (the User-Agent must not break a fetch)."""
    try:
        from pdfapp import __version__

        return __version__
    except Exception:  # noqa: BLE001 - a version read must never break the fetch
        return UNKNOWN_VERSION


# --- build kind ---------------------------------------------------------
def install_kind() -> str:
    """Which kind of build is running: installed, portable, or a dev checkout."""
    if not getattr(sys, "frozen", False):
        return DEV
    return PORTABLE if portable.is_portable() else INSTALLED


def can_self_update(kind: str | None = None) -> bool:
    """True only for the installed build, the one kind that can upgrade in place."""
    return (kind or install_kind()) == INSTALLED


# --- the notify decision ------------------------------------------------
def should_notify(
    current: str | None,
    latest: str | None,
    *,
    skipped: str | None = None,
    snooze_until: str | None = None,
    today: date | None = None,
    kind: str | None = None,
) -> bool:
    """Whether an automatic check should raise the banner for ``latest``.

    The decision table from docs/update-plan.md section 3.2, in order: the build
    must be frozen, our version must be known, ``latest`` must be newer, it must
    also be newer than any skipped version, and any snooze must have expired.

    The MANUAL check in the About dialog deliberately does not call this — it
    reports what it found regardless of skip and snooze.
    """
    if (kind or install_kind()) == DEV:
        return False
    if not is_newer(current, latest):
        return False
    # "Skip this version" is quiet until something NEWER than the skipped
    # version ships, at which point the skip is superseded on its own. The
    # parse guard is what makes corrupt state fail OPEN: an unreadable skip
    # value would otherwise silence the banner for every future release.
    # Compared with `_greater`, NOT `is_newer`: the latter refuses to treat
    # "0.0.0" as a real version (that guard is about OUR version), so a skip
    # stored as "0.0.0" would have silenced every release from then on.
    skipped_parts = parse_version(skipped)
    latest_parts = parse_version(latest)
    if skipped_parts is not None and latest_parts is not None:
        if not _greater(skipped_parts, latest_parts):
            return False
    return not snoozed(snooze_until, today)


# --- wording ------------------------------------------------------------
# The one source for the sentences BOTH update surfaces show — the banner and
# the About dialog's status line — so they can never drift apart. Pure, so
# tests assert the words without building a widget.
def available_message(current: str, latest: str) -> str:
    """The offer, shown by the banner and by the About dialog alike."""
    return f"Version {latest} is available. You have {current}."


def current_message(current: str) -> str:
    """Said only after a check actually proved it — never assumed."""
    return f"You're on the latest version ({current})."


def unreachable_message() -> str:
    """A check that could not reach the feed. Says what happened, and claims
    nothing about whether an update exists."""
    return "Couldn't reach github.com to check for updates."


def snoozed(snooze_until: str | None, today: date | None = None) -> bool:
    """True while a "remind me in 7 days" deferral is still running.

    An unset or unparseable value is not a snooze (corrupt state fails open).
    """
    if not snooze_until:
        return False
    try:
        until = date.fromisoformat(str(snooze_until))
    except (TypeError, ValueError):
        return False
    return (today or date.today()) < until


def snooze_date(today: date | None = None, days: int = SNOOZE_DAYS) -> str:
    """The value to store for "remind me in 7 days" (an ISO date string)."""
    return ((today or date.today()) + timedelta(days=days)).isoformat()


# --- downloading and running the installer ------------------------------
# What the app passes the downloaded setup. /SILENT rather than /VERYSILENT is
# deliberate: the user has just watched their app close, and the installer's
# progress bar is the evidence that something is happening. /RELAUNCH=1 is our
# own parameter, which the .iss reads via {param:RELAUNCH|0} to fire a [Run]
# entry that starts the app again — a plain hand-run of the setup passes no such
# parameter and behaves exactly as it always has.
SILENT_FLAGS = ("/SILENT", "/NORESTART", "/RELAUNCH=1")

_DOWNLOAD_DIRNAME = "PDF Editor Updates"
_CHUNK = 256 * 1024


class UpdateCancelled(Exception):
    """The user cancelled the download. Not an error — nothing is reported."""


def download_dir() -> Path:
    """Where update downloads land: a folder of our own under the user's TEMP."""
    base = os.environ.get("TEMP") or os.environ.get("TMP") or "."
    return Path(base) / _DOWNLOAD_DIRNAME


def installer_destination(info: UpdateInfo, directory: Path | None = None) -> Path:
    """The path the installer for ``info`` downloads to."""
    name = info.installer_name or f"pdf-editor-setup-{info.version}.exe"
    return (directory or download_dir()) / name


def usable_download(dest: Path, expected_size: int) -> bool:
    """True when a previous attempt already left a complete file here.

    Only ever true for an exact size match, so a truncated download can never
    be mistaken for a finished one. With no expected size (a release whose asset
    carried none) this is always False: re-downloading beats running a file we
    cannot vouch for.
    """
    if expected_size <= 0:
        return False
    try:
        return dest.is_file() and dest.stat().st_size == expected_size
    except OSError:
        return False


def download_installer(
    info: UpdateInfo,
    dest: Path,
    *,
    on_progress: Any = None,
    should_cancel: Any = None,
    timeout: float = FETCH_TIMEOUT,
) -> Path:
    """Download ``info``'s installer to ``dest`` and return the path.

    Downloads to a ``.part`` file and renames only on success, so an interrupted
    attempt can never be picked up later as a complete installer. ``on_progress``
    is called with ``(bytes_done, total)``; ``should_cancel`` is polled each
    chunk and raises :class:`UpdateCancelled` when it returns True.

    Raises ValueError when the finished file's length does not match the
    expected size, and ALSO when no expected size can be established at all.
    That second refusal matters: this file gets EXECUTED, and a truncated HTTP
    body does not raise — ``read()`` simply returns empty and the loop ends
    normally — so without a length to check against, a half-downloaded
    installer would be renamed into place and run. Length is the honest limit
    of what we can verify without published checksums (docs/update-plan.md
    section 8); having none at all is not a licence to skip verifying.
    """
    if not info.installer_url:
        raise ValueError("this release has no installer to download")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if usable_download(dest, info.installer_size):
        return dest  # a previous attempt already finished this exact file

    partial = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(
        info.installer_url, headers={"User-Agent": f"PDFEditor/{_current_version()}"}
    )
    written = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            # The release feed's size when we have one, else what the server
            # declares. Either is a real check; neither means we refuse.
            expected = info.installer_size or _content_length(response)
            if expected <= 0:
                raise ValueError(
                    "the download size could not be determined, so the installer cannot be verified"
                )
            with open(partial, "wb") as handle:
                while True:
                    if should_cancel is not None and should_cancel():
                        raise UpdateCancelled
                    chunk = response.read(_CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    written += len(chunk)
                    if on_progress is not None:
                        on_progress(written, expected)
    except BaseException:
        partial.unlink(missing_ok=True)  # never leave a partial behind
        raise

    if written != expected:
        partial.unlink(missing_ok=True)
        raise ValueError(f"the download was {written} bytes but should have been {expected}")
    os.replace(partial, dest)
    return dest


def _content_length(response: Any) -> int:
    """The server's declared length, or 0 — only ever used to size a progress bar."""
    try:
        return int(response.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        return 0


def installer_command(path: Path) -> list[str]:
    """The exact command used to run a downloaded installer. Pure, so a test can
    assert the flags without launching anything."""
    return [str(path), *SILENT_FLAGS]


def launch_installer(path: Path) -> None:
    """Start the installer detached and return immediately.

    Detached because this process is on its way out: the installer must outlive
    it. The .iss keeps ``CloseApplications=yes`` as the safety net for the
    moment between our exit and its first file copy.
    """
    creationflags = 0
    if sys.platform == "win32":  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        creationflags = 0x00000008 | 0x00000200
    subprocess.Popen(  # noqa: S603
        installer_command(path), close_fds=True, creationflags=creationflags
    )


# --- the automatic-check throttle --------------------------------------
def check_disabled() -> bool:
    """True when the automatic launch check is switched off by environment."""
    return bool(os.environ.get(DISABLE_ENV))


def check_due(last_check: str | None, now: datetime | None = None) -> bool:
    """Whether the automatic check may run — at most once every 24 hours.

    An unset or unparseable timestamp means due (fail open, same as the
    deferral state). A timestamp in the FUTURE — a clock that was wound back,
    or a settings file copied from another machine — also reads as due rather
    than locking the check out until that future arrives.
    """
    if not last_check:
        return True
    try:
        stamp = datetime.fromisoformat(str(last_check))
    except (TypeError, ValueError):
        return True
    moment = now or datetime.now(UTC)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    if stamp > moment:
        return True
    return moment - stamp >= timedelta(hours=CHECK_INTERVAL_HOURS)


def check_stamp(now: datetime | None = None) -> str:
    """The value to store as the last-check time (an ISO UTC timestamp)."""
    return (now or datetime.now(UTC)).isoformat()
