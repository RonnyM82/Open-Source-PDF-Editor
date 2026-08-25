"""Offscreen tests for the update check and its banner (UP2).

No test touches the network: ``updates.fetch_latest_release`` is monkeypatched
everywhere, and the checker's synchronous body (``run_now``) is driven directly
rather than through a thread, which is the same convention the dialog-bearing
flows use elsewhere in this suite.

The autouse ``_isolate_app_data`` fixture (conftest) points LOCALAPPDATA at
tmp_path, so the settings written here — the skip and snooze keys especially —
never reach the developer's real profile.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

pytest.importorskip("PySide6")

import pdfapp.main_window as mw  # noqa: E402
from pdfapp import updates  # noqa: E402
from pdfapp.main_window import MainWindow  # noqa: E402
from pdfapp.update_banner import DOWNLOAD_PAGE, UPDATE_NOW, UpdateBanner  # noqa: E402
from pdfapp.update_check import UpdateChecker  # noqa: E402

NEWER = updates.UpdateInfo(
    version="99.0.0",
    page_url="https://example.com/releases/tag/v99.0.0",
    installer_name="pdf-editor-setup-99.0.0.exe",
    installer_url="https://example.com/pdf-editor-setup-99.0.0.exe",
    installer_size=1234,
)
OLDER = updates.UpdateInfo(version="0.0.1", page_url="https://example.com/old")


def _payload(version: str) -> dict:
    return {
        "tag_name": f"v{version}",
        "html_url": f"https://example.com/releases/tag/v{version}",
        "assets": [
            {
                "name": f"pdf-editor-setup-{version}.exe",
                "browser_download_url": f"https://example.com/setup-{version}.exe",
                "size": 99,
            }
        ],
    }


def _installed(monkeypatch):
    """Make the window behave as the installed build (the only self-updating kind)."""
    monkeypatch.setattr(mw.updates, "install_kind", lambda: updates.INSTALLED)
    monkeypatch.setattr(mw.updates, "can_self_update", lambda *a: True)


@pytest.fixture(autouse=True)
def _no_real_timers(monkeypatch):
    """Never let this file arm a real QTimer.

    ``schedule_update_check`` posts a 3-second single-shot. Left real, a timer
    armed by one test fires during a LATER one — and if that later test still
    has the build kind patched to "installed", the deferred check would sail
    past its gates and make a live request to GitHub. Recording the call
    instead keeps the suite deterministic and provably offline.
    """
    scheduled = []
    monkeypatch.setattr(mw.QTimer, "singleShot", lambda ms, fn: scheduled.append((ms, fn)))
    return scheduled


# --- the checker bridge -------------------------------------------------
def test_checker_emits_the_parsed_release(qapp, monkeypatch):
    monkeypatch.setattr(updates, "fetch_latest_release", lambda url=None: _payload("1.2.3"))
    checker = UpdateChecker()
    seen = []
    checker.finished.connect(seen.append)
    checker.run_now()
    assert [i.version for i in seen] == ["1.2.3"]


def test_checker_reports_a_network_failure_instead_of_raising(qapp, monkeypatch):
    def boom(url=None):
        raise OSError("no route to host")

    monkeypatch.setattr(updates, "fetch_latest_release", boom)
    checker = UpdateChecker()
    failures = []
    checker.failed.connect(failures.append)
    checker.run_now()  # must not raise
    assert failures and "no route to host" in failures[0]


def test_checker_reports_an_unreadable_feed(qapp, monkeypatch):
    monkeypatch.setattr(updates, "fetch_latest_release", lambda url=None: {"tag_name": "nightly"})
    checker = UpdateChecker()
    failures = []
    checker.failed.connect(failures.append)
    checker.run_now()
    assert failures


def test_checker_emits_exactly_one_signal(qapp, monkeypatch):
    monkeypatch.setattr(updates, "fetch_latest_release", lambda url=None: _payload("1.0.0"))
    checker = UpdateChecker()
    calls = []
    checker.finished.connect(lambda i: calls.append("finished"))
    checker.failed.connect(lambda r: calls.append("failed"))
    checker.run_now()
    assert calls == ["finished"]


def test_abandoned_checker_delivers_nothing(qapp, monkeypatch):
    """A check still in flight when the window closes must not deliver."""
    monkeypatch.setattr(updates, "fetch_latest_release", lambda url=None: _payload("1.0.0"))
    checker = UpdateChecker()
    seen = []
    checker.finished.connect(seen.append)
    checker.abandon()
    checker.run_now()
    assert seen == []


def test_second_start_is_refused_while_one_runs(qapp):
    checker = UpdateChecker()
    checker._running = True
    assert checker.start() is False


# --- the banner widget --------------------------------------------------
def test_banner_offers_the_in_place_upgrade_when_it_can(qapp):
    banner = UpdateBanner()
    banner.present(NEWER, "0.11.0", can_update=True)
    assert banner.primary_text() == UPDATE_NOW
    assert "99.0.0" in banner.message()
    assert "0.11.0" in banner.message()


def test_banner_offers_the_download_page_when_it_cannot(qapp):
    """Portable builds, and releases carrying no installer asset, get the page."""
    banner = UpdateBanner()
    banner.present(NEWER, "0.11.0", can_update=False)
    assert banner.primary_text() == DOWNLOAD_PAGE


@pytest.mark.parametrize(
    ("button", "signal_name"),
    [
        ("_remind", "remindLaterRequested"),
        ("_skip", "skipRequested"),
        ("_close", "dismissed"),
    ],
)
def test_banner_buttons_emit_and_hide(qapp, button, signal_name):
    banner = UpdateBanner()
    banner.present(NEWER, "0.11.0", can_update=True)
    fired = []
    getattr(banner, signal_name).connect(lambda: fired.append(signal_name))
    getattr(banner, button).click()
    assert fired == [signal_name]
    assert banner.isVisible() is False


# --- the window's gates on the automatic check --------------------------
def test_automatic_check_is_skipped_for_a_dev_build(qapp, monkeypatch):
    """A source checkout checks manually from the About dialog. This is also
    what keeps the whole offscreen suite off the network."""
    monkeypatch.setattr(mw.updates, "install_kind", lambda: updates.DEV)
    window = MainWindow()
    try:
        assert window.schedule_update_check() is False
    finally:
        window.close()


def test_automatic_check_honours_the_kill_switch(qapp, monkeypatch):
    _installed(monkeypatch)
    monkeypatch.setenv(updates.DISABLE_ENV, "1")
    window = MainWindow()
    try:
        assert window.schedule_update_check() is False
    finally:
        window.close()


def test_automatic_check_is_throttled_to_once_a_day(qapp, monkeypatch):
    _installed(monkeypatch)
    monkeypatch.delenv(updates.DISABLE_ENV, raising=False)
    window = MainWindow()
    try:
        assert window.schedule_update_check() is True  # never checked
        window._settings.set(updates.LAST_CHECK_KEY, updates.check_stamp())
        assert window.schedule_update_check() is False  # just checked
        stale = datetime.now(UTC) - timedelta(hours=25)
        window._settings.set(updates.LAST_CHECK_KEY, updates.check_stamp(stale))
        assert window.schedule_update_check() is True
    finally:
        window.close()


def test_scheduling_arms_a_deferred_check(qapp, monkeypatch, _no_real_timers):
    """The check is deferred, not run inline: startup must never wait on the
    network. The armed callback is the one that actually fetches."""
    _installed(monkeypatch)
    monkeypatch.delenv(updates.DISABLE_ENV, raising=False)
    window = MainWindow()
    try:
        assert window.schedule_update_check() is True
        assert len(_no_real_timers) == 1
        delay, callback = _no_real_timers[0]
        assert delay > 0
        assert callback == window._run_automatic_update_check
    finally:
        window.close()


def test_check_stamp_is_written_before_the_fetch(qapp, monkeypatch):
    """A feed that hangs or fails must not make every launch retry it."""
    _installed(monkeypatch)
    monkeypatch.delenv(updates.DISABLE_ENV, raising=False)
    window = MainWindow()
    try:
        monkeypatch.setattr(window._update_checker, "start", lambda *a: True)
        window._run_automatic_update_check()
        assert window._settings.get(updates.LAST_CHECK_KEY)
    finally:
        window.close()


# --- what the window does with a result ---------------------------------
def test_newer_release_raises_the_banner(qapp, monkeypatch):
    _installed(monkeypatch)
    window = MainWindow()
    try:
        window._on_update_found(NEWER)
        assert window._update_banner.info is NEWER
        assert window._update_info is NEWER
    finally:
        window.close()


def test_older_release_raises_nothing(qapp, monkeypatch):
    _installed(monkeypatch)
    window = MainWindow()
    try:
        window._on_update_found(OLDER)
        assert window._update_banner.info is None
        # ...but the check itself is recorded, so About can say "you're current".
        assert window._update_info is OLDER
        assert window._update_checked is True
    finally:
        window.close()


def test_skipped_version_does_not_raise_the_banner(qapp, monkeypatch):
    _installed(monkeypatch)
    window = MainWindow()
    try:
        window._settings.set(updates.SKIPPED_VERSION_KEY, NEWER.version)
        window._on_update_found(NEWER)
        assert window._update_banner.info is None
    finally:
        window.close()


def test_snoozed_check_does_not_raise_the_banner(qapp, monkeypatch):
    _installed(monkeypatch)
    window = MainWindow()
    try:
        window._settings.set(updates.SNOOZE_UNTIL_KEY, updates.snooze_date())
        window._on_update_found(NEWER)
        assert window._update_banner.info is None
    finally:
        window.close()


def test_failed_check_says_nothing_to_the_user(qapp, monkeypatch):
    _installed(monkeypatch)
    window = MainWindow()
    try:
        window._on_update_check_failed("no route to host")
        assert window._update_banner.info is None
        assert window._update_error == "no route to host"
        assert window._update_checked is True
    finally:
        window.close()


# --- the two deferrals, written through the real buttons ----------------
def test_skip_button_records_the_offered_version(qapp, monkeypatch):
    _installed(monkeypatch)
    window = MainWindow()
    try:
        window._on_update_found(NEWER)
        window._update_banner._skip.click()
        assert window._settings.get(updates.SKIPPED_VERSION_KEY) == "99.0.0"
        assert window._settings.get(updates.SNOOZE_UNTIL_KEY) is None
    finally:
        window.close()


def test_remind_button_records_a_date_seven_days_out(qapp, monkeypatch):
    _installed(monkeypatch)
    window = MainWindow()
    try:
        window._on_update_found(NEWER)
        window._update_banner._remind.click()
        stored = window._settings.get(updates.SNOOZE_UNTIL_KEY)
        assert stored == (date.today() + timedelta(days=7)).isoformat()
        assert window._settings.get(updates.SKIPPED_VERSION_KEY) is None
    finally:
        window.close()


def test_close_cross_records_nothing(qapp, monkeypatch):
    """The banner simply returns at the next launch."""
    _installed(monkeypatch)
    window = MainWindow()
    try:
        window._on_update_found(NEWER)
        window._update_banner._close.click()
        assert window._settings.get(updates.SKIPPED_VERSION_KEY) is None
        assert window._settings.get(updates.SNOOZE_UNTIL_KEY) is None
    finally:
        window.close()


def test_skipping_then_a_newer_release_offers_again(qapp, monkeypatch):
    """The whole point of skip-by-version: it expires on its own."""
    _installed(monkeypatch)
    window = MainWindow()
    try:
        window._on_update_found(NEWER)
        window._update_banner._skip.click()
        newest = updates.UpdateInfo(version="99.1.0", page_url="https://example.com/newest")
        window._on_update_found(newest)
        assert window._update_banner.info is newest
    finally:
        window.close()


# --- the in-place upgrade flow (UP4) ------------------------------------
def _served(tmp_path, payload=b"setup"):
    """An UpdateInfo whose installer is a real local file (a file:// download)."""
    source = tmp_path / "pdf-editor-setup-99.0.0.exe"
    source.write_bytes(payload)
    return updates.UpdateInfo(
        version="99.0.0",
        page_url="https://example.com/v99",
        installer_name="pdf-editor-setup-99.0.0.exe",
        installer_url=source.as_uri(),
        installer_size=len(payload),
    )


def test_update_downloads_then_closes_then_launches(qapp, monkeypatch, tmp_path):
    """The ordering IS the safety: nothing is launched until the window has
    actually agreed to close."""
    _installed(monkeypatch)
    window = MainWindow()
    try:
        order = []
        window._update_info = _served(tmp_path)
        monkeypatch.setattr(
            mw.updates,
            "installer_destination",
            lambda info, d=None: tmp_path / "downloaded.exe",
        )
        real_download = mw.updates.download_installer

        def traced_download(info, dest, **kw):
            order.append("download")
            return real_download(info, dest, **kw)

        monkeypatch.setattr(mw.updates, "download_installer", traced_download)
        monkeypatch.setattr(MainWindow, "close", lambda self: order.append("close") or True)
        monkeypatch.setattr(mw.updates, "launch_installer", lambda p: order.append("launch"))
        window._start_update()
        assert order == ["download", "close", "launch"]
    finally:
        MainWindow.close = mw.QMainWindow.close
        window.close()


def test_cancelling_the_close_launches_nothing(qapp, monkeypatch, tmp_path):
    """A document with unsaved changes the user backs out of stops the update
    dead — and the downloaded file just waits for next time."""
    _installed(monkeypatch)
    window = MainWindow()
    try:
        window._update_info = _served(tmp_path)
        monkeypatch.setattr(
            mw.updates, "installer_destination", lambda info, d=None: tmp_path / "d.exe"
        )
        launched = []
        monkeypatch.setattr(MainWindow, "close", lambda self: False)  # user cancelled
        monkeypatch.setattr(mw.updates, "launch_installer", lambda p: launched.append(p))
        window._start_update()
        assert launched == []
        assert (tmp_path / "d.exe").exists()  # the download survives for next time
    finally:
        MainWindow.close = mw.QMainWindow.close
        window.close()


def test_a_failed_download_launches_nothing_and_says_why(qapp, monkeypatch, tmp_path):
    _installed(monkeypatch)
    window = MainWindow()
    try:
        window._update_info = _served(tmp_path)
        monkeypatch.setattr(
            mw.updates, "installer_destination", lambda info, d=None: tmp_path / "d.exe"
        )

        def boom(*a, **k):
            raise OSError("the connection dropped")

        monkeypatch.setattr(mw.updates, "download_installer", boom)
        launched = []
        monkeypatch.setattr(mw.updates, "launch_installer", lambda p: launched.append(p))
        window._start_update()
        assert launched == []
        assert "connection dropped" in window.statusBar().currentMessage()
    finally:
        window.close()


def test_a_cancelled_download_is_silent(qapp, monkeypatch, tmp_path):
    """Cancelling is a choice, not a failure — nothing is reported."""
    _installed(monkeypatch)
    window = MainWindow()
    try:
        window._update_info = _served(tmp_path)
        monkeypatch.setattr(
            mw.updates, "installer_destination", lambda info, d=None: tmp_path / "d.exe"
        )

        def cancelled(*a, **k):
            raise mw.updates.UpdateCancelled

        monkeypatch.setattr(mw.updates, "download_installer", cancelled)
        launched = []
        monkeypatch.setattr(mw.updates, "launch_installer", lambda p: launched.append(p))
        window._start_update()
        assert launched == []
        # The idle status message is untouched — no failure was reported.
        assert "could not be downloaded" not in window.statusBar().currentMessage()
    finally:
        window.close()


def test_portable_build_opens_the_page_and_downloads_nothing(qapp, monkeypatch, tmp_path):
    """The portable exe cannot replace itself while running, so it never tries."""
    monkeypatch.setattr(mw.updates, "install_kind", lambda: updates.PORTABLE)
    monkeypatch.setattr(mw.updates, "can_self_update", lambda *a: False)
    window = MainWindow()
    try:
        window._update_info = _served(tmp_path)
        downloads, opened = [], []
        monkeypatch.setattr(mw.updates, "download_installer", lambda *a, **k: downloads.append(a))
        monkeypatch.setattr(
            mw.QDesktopServices, "openUrl", lambda url: opened.append(url.toString())
        )
        window._start_update()
        assert downloads == []
        assert opened == ["https://example.com/v99"]
    finally:
        window.close()


def test_a_release_without_an_installer_opens_the_page(qapp, monkeypatch):
    _installed(monkeypatch)
    window = MainWindow()
    try:
        window._update_info = updates.UpdateInfo(
            version="99.0.0", page_url="https://example.com/v99"
        )
        opened = []
        monkeypatch.setattr(
            mw.QDesktopServices, "openUrl", lambda url: opened.append(url.toString())
        )
        window._start_update()
        assert opened == ["https://example.com/v99"]
    finally:
        window.close()


def test_update_with_nothing_found_does_nothing(qapp, monkeypatch):
    _installed(monkeypatch)
    window = MainWindow()
    try:
        launched = []
        monkeypatch.setattr(mw.updates, "launch_installer", lambda p: launched.append(p))
        window._start_update()  # no _update_info at all
        assert launched == []
    finally:
        window.close()


# --- the banner's place in the window -----------------------------------
def test_banner_lives_above_the_tabs(qapp):
    """An available update is a fact about the app, not about one document."""
    window = MainWindow()
    try:
        central = window.centralWidget()
        layout = central.layout()
        assert layout.indexOf(window._update_banner) < layout.indexOf(window._tabs)
    finally:
        window.close()


def test_theme_change_restyles_the_banner(qapp, monkeypatch):
    window = MainWindow()
    try:
        monkeypatch.setattr(mw.theme, "apply_theme", lambda *a, **k: None)
        window._update_banner.present(NEWER, "0.11.0", can_update=True)
        window._on_theme_changed(mw.theme.LIGHT)
        assert window._update_banner.styleSheet()
    finally:
        window.close()
