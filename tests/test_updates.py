"""Pure-logic tests for update checking (pdfapp.updates). Qt-free, no network.

The one network seam is ``fetch_latest_release``; nothing here calls it against
a real URL. ``LATEST_RELEASE_PAYLOAD`` below is a real ``releases/latest``
response captured from the GitHub API on 2026-08-26 (trimmed to the fields the
parser reads, plus a few it must ignore).
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime, timedelta

import pytest

from pdfapp import portable, updates

# --- the pinned real API response ---------------------------------------
LATEST_RELEASE_PAYLOAD = {
    "tag_name": "v0.11.0",
    "name": "PDF Editor v0.11.0",
    "html_url": "https://github.com/RonnyM82/Open-Source-PDF-Editor/releases/tag/v0.11.0",
    "draft": False,
    "prerelease": False,
    "published_at": "2026-08-18T06:19:42Z",
    "body": "Release notes go here.",
    "assets": [
        {
            "name": "pdf-editor-portable-0.11.0.zip",
            "browser_download_url": (
                "https://github.com/RonnyM82/Open-Source-PDF-Editor/releases/download/"
                "v0.11.0/pdf-editor-portable-0.11.0.zip"
            ),
            "size": 137998819,
            "content_type": "application/zip",
            "state": "uploaded",
        },
        {
            "name": "pdf-editor-setup-0.11.0.exe",
            "browser_download_url": (
                "https://github.com/RonnyM82/Open-Source-PDF-Editor/releases/download/"
                "v0.11.0/pdf-editor-setup-0.11.0.exe"
            ),
            "size": 94204796,
            "content_type": "application/x-msdownload",
            "state": "uploaded",
        },
    ],
}


# --- version parsing ----------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("v0.11.0", (0, 11, 0)),
        ("0.11.0", (0, 11, 0)),
        ("V1.2.3", (1, 2, 3)),
        ("  v2.0  ", (2, 0)),
        ("10.20.30", (10, 20, 30)),
    ],
)
def test_parse_version_accepts_plain_dotted_numbers(text, expected):
    assert updates.parse_version(text) == expected


@pytest.mark.parametrize(
    "text",
    ["v1.0.0-rc1", "1.0.0b2", "", "v", "abc", "1..2", "1.2.", None, 11, "v1,2,3"],
)
def test_parse_version_rejects_anything_else(text):
    """A tag we don't understand must read as None — every caller treats that
    as "no update", the conservative direction."""
    assert updates.parse_version(text) is None


# --- newer-than comparison ----------------------------------------------
@pytest.mark.parametrize(
    ("current", "latest"),
    [
        ("0.11.0", "0.12.0"),
        ("0.11.0", "1.0.0"),
        ("0.11.0", "0.11.1"),
        ("0.9.0", "0.10.0"),  # numeric, not lexicographic
        ("0.11", "0.11.1"),
    ],
)
def test_is_newer_true(current, latest):
    assert updates.is_newer(current, latest) is True


@pytest.mark.parametrize(
    ("current", "latest"),
    [
        ("0.11.0", "0.11.0"),
        ("0.11.0", "0.10.9"),
        ("0.12.0", "0.11.0"),
        ("0.11.0", "0.11"),  # padded compare: 0.11 == 0.11.0
        ("0.11.0", "v1.0.0-rc1"),  # unparseable latest
        ("not-a-version", "0.12.0"),
    ],
)
def test_is_newer_false(current, latest):
    assert updates.is_newer(current, latest) is False


def test_unknown_current_version_is_never_out_of_date():
    """pdfcore.version falls back to 0.0.0 when it can't read a version. A
    build in that state must not nag about an upgrade forever."""
    assert updates.is_newer(updates.UNKNOWN_VERSION, "9.9.9") is False


# --- the release feed ---------------------------------------------------
def test_release_to_info_reads_the_real_payload():
    info = updates.release_to_info(LATEST_RELEASE_PAYLOAD)
    assert info is not None
    assert info.version == "0.11.0"  # the "v" prefix is stripped
    assert info.page_url.endswith("/releases/tag/v0.11.0")
    assert info.installer_name == "pdf-editor-setup-0.11.0.exe"
    assert info.installer_url.endswith("/pdf-editor-setup-0.11.0.exe")
    assert info.installer_size == 94204796


def test_release_to_info_picks_the_installer_not_the_zip():
    """The asset is matched by its exact expected name — never by position, and
    never by grabbing whichever asset happens to be an executable."""
    payload = dict(LATEST_RELEASE_PAYLOAD)
    payload["assets"] = [
        {"name": "notes.exe", "browser_download_url": "https://x/notes.exe", "size": 5},
        *LATEST_RELEASE_PAYLOAD["assets"],
    ]
    info = updates.release_to_info(payload)
    assert info.installer_name == "pdf-editor-setup-0.11.0.exe"


def test_release_with_no_installer_asset_still_reports_the_version():
    """A release carrying only the portable ZIP is a real update — the UI just
    offers the download page instead of an in-place upgrade."""
    payload = dict(LATEST_RELEASE_PAYLOAD)
    payload["assets"] = [LATEST_RELEASE_PAYLOAD["assets"][0]]  # the zip only
    info = updates.release_to_info(payload)
    assert info.version == "0.11.0"
    assert info.installer_url is None
    assert info.installer_size == 0


def test_release_asset_without_a_url_is_ignored():
    payload = dict(LATEST_RELEASE_PAYLOAD)
    payload["assets"] = [{"name": "pdf-editor-setup-0.11.0.exe", "size": 10}]
    assert updates.release_to_info(payload).installer_url is None


def test_release_asset_with_a_junk_size_reads_as_unknown():
    """Size 0 means "the feed gave us no length to check against". The download
    then falls back to the server's Content-Length, and refuses outright if
    there is none either — see test_an_unsizeable_download_is_refused_not_run."""
    payload = dict(LATEST_RELEASE_PAYLOAD)
    payload["assets"] = [
        {
            "name": "pdf-editor-setup-0.11.0.exe",
            "browser_download_url": "https://x/pdf-editor-setup-0.11.0.exe",
            "size": None,
        }
    ]
    assert updates.release_to_info(payload).installer_size == 0


@pytest.mark.parametrize(
    "payload",
    [None, [], "release", {}, {"tag_name": "nightly"}, {"tag_name": None}],
)
def test_release_to_info_rejects_junk(payload):
    assert updates.release_to_info(payload) is None


def test_release_without_html_url_falls_back_to_the_releases_page():
    info = updates.release_to_info({"tag_name": "v1.0.0"})
    assert info.page_url == updates.RELEASES_PAGE_URL


# --- the feed URL and the fetch seam ------------------------------------
def test_feed_url_defaults_to_the_github_api(monkeypatch):
    monkeypatch.delenv(updates.FEED_ENV, raising=False)
    assert updates.feed_url() == updates.DEFAULT_FEED_URL


def test_feed_url_honours_the_override(monkeypatch):
    monkeypatch.setenv(updates.FEED_ENV, "https://example.com/feed.json")
    assert updates.feed_url() == "https://example.com/feed.json"


def test_fetch_reads_a_local_feed_file(tmp_path, monkeypatch):
    """The override may name a local file, which is how the frozen build gets
    walked end to end against a locally built installer (no network at all)."""
    feed = tmp_path / "feed.json"
    feed.write_text(json.dumps(LATEST_RELEASE_PAYLOAD), encoding="utf-8")
    monkeypatch.setenv(updates.FEED_ENV, str(feed))
    assert updates.release_to_info(updates.fetch_latest_release()).version == "0.11.0"


# --- which build is running ---------------------------------------------
def test_install_kind_dev_when_not_frozen(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert updates.install_kind() == updates.DEV
    assert updates.can_self_update() is False


def test_install_kind_installed_when_frozen_without_the_marker(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(portable, "is_portable", lambda: False)
    assert updates.install_kind() == updates.INSTALLED
    assert updates.can_self_update() is True


def test_install_kind_portable_when_the_marker_is_present(monkeypatch):
    """The portable exe can't replace itself while running, so it never
    self-updates — it gets the banner and the download page."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(portable, "is_portable", lambda: True)
    assert updates.install_kind() == updates.PORTABLE
    assert updates.can_self_update() is False


# --- the notify decision table ------------------------------------------
def test_notifies_on_a_newer_release():
    assert updates.should_notify("0.11.0", "0.12.0", kind=updates.INSTALLED) is True


def test_portable_build_is_notified_too():
    """Portable can't upgrade in place, but it is still told an update exists."""
    assert updates.should_notify("0.11.0", "0.12.0", kind=updates.PORTABLE) is True


def test_dev_build_is_never_notified_automatically():
    """A source checkout checks manually from the About dialog; nagging it
    helps nobody."""
    assert updates.should_notify("0.11.0", "0.12.0", kind=updates.DEV) is False


def test_no_notify_when_already_current():
    assert updates.should_notify("0.12.0", "0.12.0", kind=updates.INSTALLED) is False


def test_no_notify_when_our_version_is_unknown():
    assert updates.should_notify(updates.UNKNOWN_VERSION, "0.12.0", kind=updates.INSTALLED) is False


def test_skipped_version_is_not_offered_again():
    assert (
        updates.should_notify("0.11.0", "0.12.0", skipped="0.12.0", kind=updates.INSTALLED) is False
    )


def test_skip_is_superseded_by_the_next_release():
    """Scott's spec: "skip this version" is quiet until the version AFTER it
    ships, then the banner returns on its own."""
    assert (
        updates.should_notify("0.11.0", "0.13.0", skipped="0.12.0", kind=updates.INSTALLED) is True
    )


def test_a_skipped_version_of_zero_does_not_silence_everything():
    """`is_newer` refuses to treat "0.0.0" as a real version, but that guard is
    about OUR OWN version. Routing the skip comparison through it made every
    future release read as not-newer and silenced the banner permanently."""
    assert (
        updates.should_notify("0.11.0", "0.12.0", skipped="0.0.0", kind=updates.INSTALLED) is True
    )


def test_corrupt_skipped_version_is_ignored():
    """Corrupt deferral state fails OPEN — one bad write must not silence
    updates permanently."""
    assert (
        updates.should_notify("0.11.0", "0.12.0", skipped="garbage", kind=updates.INSTALLED) is True
    )


def test_snooze_holds_the_banner_back():
    today = date(2026, 8, 26)
    until = updates.snooze_date(today)
    assert until == "2026-09-02"  # seven days
    assert (
        updates.should_notify(
            "0.11.0", "0.12.0", snooze_until=until, today=today, kind=updates.INSTALLED
        )
        is False
    )


def test_snooze_expires_and_the_banner_returns():
    until = updates.snooze_date(date(2026, 8, 26))
    assert (
        updates.should_notify(
            "0.11.0",
            "0.12.0",
            snooze_until=until,
            today=date(2026, 9, 2),
            kind=updates.INSTALLED,
        )
        is True
    )


def test_snooze_silences_a_newer_release_too():
    """Unlike skip, a snooze is about time, not about one version: a release
    that lands during the week stays quiet until the week is up."""
    assert (
        updates.should_notify(
            "0.11.0",
            "0.13.0",
            snooze_until="2026-09-02",
            today=date(2026, 8, 27),
            kind=updates.INSTALLED,
        )
        is False
    )


def test_skip_and_snooze_are_independent():
    """Both are stored; neither clears the other, and both must pass."""
    args = {"skipped": "0.12.0", "snooze_until": "2026-09-02", "kind": updates.INSTALLED}
    # Snooze expired, but 0.12.0 is still the skipped version.
    assert updates.should_notify("0.11.0", "0.12.0", today=date(2026, 9, 3), **args) is False
    # Newer than the skip, but the snooze is still running.
    assert updates.should_notify("0.11.0", "0.13.0", today=date(2026, 8, 27), **args) is False
    # Both cleared.
    assert updates.should_notify("0.11.0", "0.13.0", today=date(2026, 9, 3), **args) is True


@pytest.mark.parametrize("value", [None, "", "not-a-date", "2026-13-45", 7])
def test_corrupt_snooze_is_not_a_snooze(value):
    assert updates.snoozed(value, date(2026, 8, 26)) is False


# --- the automatic-check throttle ---------------------------------------
def test_check_due_when_never_checked():
    assert updates.check_due(None) is True


def test_check_not_due_within_the_interval():
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    recent = (now - timedelta(hours=3)).isoformat()
    assert updates.check_due(recent, now) is False


def test_check_due_after_the_interval():
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    stale = (now - timedelta(hours=25)).isoformat()
    assert updates.check_due(stale, now) is True


def test_check_due_when_the_stamp_is_in_the_future():
    """A clock wound back (or a settings file copied from another machine)
    must not lock the check out until that future arrives."""
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    ahead = (now + timedelta(days=30)).isoformat()
    assert updates.check_due(ahead, now) is True


@pytest.mark.parametrize("value", ["", "yesterday", "2026-13-45T99:99", 5])
def test_check_due_on_corrupt_stamp(value):
    assert updates.check_due(value, datetime(2026, 8, 26, tzinfo=UTC)) is True


def test_check_stamp_round_trips_through_check_due():
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    assert updates.check_due(updates.check_stamp(now), now) is False


def test_naive_stamp_is_treated_as_utc():
    """Settings written by an older build (or hand-edited) may carry no zone;
    comparing a naive stamp against an aware clock would otherwise raise."""
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    assert updates.check_due("2026-08-26T09:00:00", now) is False
    assert updates.check_due("2026-08-20T09:00:00", now) is True


# --- downloading the installer ------------------------------------------
def _local_release(tmp_path, payload: bytes) -> updates.UpdateInfo:
    """An UpdateInfo whose installer is a real file on disk, served as a
    file:// URL — a genuine download through urllib, with no network."""
    source = tmp_path / "served" / "pdf-editor-setup-9.9.9.exe"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(payload)
    return updates.UpdateInfo(
        version="9.9.9",
        page_url="https://example.com/v9",
        installer_name="pdf-editor-setup-9.9.9.exe",
        installer_url=source.as_uri(),
        installer_size=len(payload),
    )


def test_download_writes_the_installer(tmp_path):
    payload = b"setup bytes" * 500
    info = _local_release(tmp_path, payload)
    dest = tmp_path / "out" / "setup.exe"
    assert updates.download_installer(info, dest) == dest
    assert dest.read_bytes() == payload


def test_download_reports_progress(tmp_path):
    info = _local_release(tmp_path, b"x" * (600 * 1024))  # spans several chunks
    seen = []
    updates.download_installer(
        info, tmp_path / "setup.exe", on_progress=lambda done, total: seen.append((done, total))
    )
    assert seen[-1] == (600 * 1024, 600 * 1024)
    assert len(seen) > 1  # really chunked, not one lump


def test_download_leaves_no_partial_file_when_cancelled(tmp_path):
    """A cancelled attempt must leave nothing that a later run could mistake
    for a finished installer."""
    info = _local_release(tmp_path, b"y" * (600 * 1024))
    dest = tmp_path / "setup.exe"
    with pytest.raises(updates.UpdateCancelled):
        updates.download_installer(info, dest, should_cancel=lambda: True)
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


def test_download_refuses_a_wrong_length_file(tmp_path):
    """The honest limit of verification without published checksums."""
    payload = b"short"
    info = _local_release(tmp_path, payload)
    lying = updates.UpdateInfo(
        version=info.version,
        page_url=info.page_url,
        installer_name=info.installer_name,
        installer_url=info.installer_url,
        installer_size=len(payload) + 100,  # the feed claims more than arrives
    )
    dest = tmp_path / "setup.exe"
    with pytest.raises(ValueError, match="should have been"):
        updates.download_installer(lying, dest)
    assert not dest.exists()


def test_download_reuses_a_complete_earlier_attempt(tmp_path):
    """Same file already there at exactly the right size: don't fetch 95 MB again."""
    payload = b"already here"
    info = _local_release(tmp_path, payload)
    dest = tmp_path / "setup.exe"
    dest.write_bytes(payload)
    calls = []
    updates.download_installer(info, dest, on_progress=lambda *a: calls.append(a))
    assert calls == []  # nothing was downloaded


def test_a_truncated_leftover_is_not_reused(tmp_path):
    payload = b"the full payload"
    info = _local_release(tmp_path, payload)
    dest = tmp_path / "setup.exe"
    dest.write_bytes(payload[:4])  # a stale, truncated file
    updates.download_installer(info, dest)
    assert dest.read_bytes() == payload


def test_an_unsizeable_download_is_refused_not_run(tmp_path, monkeypatch):
    """This file gets EXECUTED. A truncated HTTP body does not raise — read()
    just returns empty and the loop ends — so with no length to check against,
    a half-downloaded installer would be renamed into place and run. Refuse
    instead."""
    payload = b"setup bytes"
    info = _local_release(tmp_path, payload)
    sizeless = updates.UpdateInfo(
        version=info.version,
        page_url=info.page_url,
        installer_name=info.installer_name,
        installer_url=info.installer_url,
        installer_size=0,  # the feed carried no usable size
    )
    dest = tmp_path / "setup.exe"
    # Serve it with no Content-Length either, so nothing can vouch for it.
    monkeypatch.setattr(updates, "_content_length", lambda response: 0)
    with pytest.raises(ValueError, match="could not be determined"):
        updates.download_installer(sizeless, dest)
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


def test_the_servers_declared_length_is_used_when_the_feed_has_none(tmp_path):
    """A release with no size is still updatable when the server declares a
    length — that is a real check, so it is allowed."""
    payload = b"setup bytes" * 40
    info = _local_release(tmp_path, payload)
    sizeless = updates.UpdateInfo(
        version=info.version,
        page_url=info.page_url,
        installer_name=info.installer_name,
        installer_url=info.installer_url,
        installer_size=0,
    )
    dest = tmp_path / "setup.exe"
    assert updates.download_installer(sizeless, dest) == dest
    assert dest.read_bytes() == payload


def test_usable_download_needs_an_expected_size(tmp_path):
    """With no size to check against, re-download rather than run a file we
    cannot vouch for."""
    dest = tmp_path / "setup.exe"
    dest.write_bytes(b"whatever")
    assert updates.usable_download(dest, 0) is False
    assert updates.usable_download(dest, 8) is True
    assert updates.usable_download(dest, 9) is False


def test_download_refuses_a_release_with_no_installer(tmp_path):
    info = updates.UpdateInfo(version="9.9.9", page_url="https://example.com/v9")
    with pytest.raises(ValueError, match="no installer"):
        updates.download_installer(info, tmp_path / "setup.exe")


def test_installer_destination_uses_the_asset_name(tmp_path):
    info = updates.UpdateInfo(
        version="9.9.9",
        page_url="https://example.com/v9",
        installer_name="pdf-editor-setup-9.9.9.exe",
        installer_url="https://example.com/s.exe",
    )
    assert updates.installer_destination(info, tmp_path).name == "pdf-editor-setup-9.9.9.exe"


# --- running the installer ----------------------------------------------
def test_installer_command_is_silent_and_asks_for_the_relaunch():
    """These exact flags are what the .iss Check function reads. /SILENT (not
    /VERYSILENT) deliberately leaves a progress bar on screen."""
    command = updates.installer_command("C:/tmp/setup.exe")
    assert command[0] == "C:/tmp/setup.exe"
    assert command[1:] == ["/SILENT", "/NORESTART", "/RELAUNCH=1"]


# --- the kill switch ----------------------------------------------------
def test_check_disabled_off_by_default(monkeypatch):
    monkeypatch.delenv(updates.DISABLE_ENV, raising=False)
    assert updates.check_disabled() is False


def test_check_disabled_by_environment(monkeypatch):
    monkeypatch.setenv(updates.DISABLE_ENV, "1")
    assert updates.check_disabled() is True
