"""Offscreen tests for the Help → About dialog."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

import pdfapp.main_window as mw  # noqa: E402
from pdfapp import __version__ as APP_VERSION  # noqa: E402
from pdfapp import updates  # noqa: E402
from pdfapp.about_dialog import (  # noqa: E402
    APP_NAME,
    CHECKING,
    NOT_CHECKED,
    AboutDialog,
    about_html,
    component_versions,
    update_status_text,
)
from pdfapp.main_window import MainWindow  # noqa: E402
from pdfapp.update_banner import DOWNLOAD_PAGE, UPDATE_NOW  # noqa: E402

NEWER = updates.UpdateInfo(
    version="99.0.0",
    page_url="https://example.com/v99",
    installer_name="pdf-editor-setup-99.0.0.exe",
    installer_url="https://example.com/setup.exe",
    installer_size=10,
)
NO_INSTALLER = updates.UpdateInfo(version="99.0.0", page_url="https://example.com/v99")


def test_component_versions_carry_app_and_stack():
    versions = component_versions()
    assert versions[APP_NAME] == APP_VERSION
    # The live stack — module attributes, so these are real, never "—".
    assert versions["PySide6"] != "—"
    assert versions["PyMuPDF"] != "—"
    assert "Python" in versions


def test_about_html_states_version_and_licence():
    html = about_html()
    assert APP_NAME in html
    assert APP_VERSION in html
    assert "AGPL-3.0" in html
    assert "PyMuPDF" in html
    assert "github.com" in html  # source-availability link


def test_source_link_is_darker_blue_in_light_theme():
    from pdfapp import theme

    # Light mode needs a darker link than dark mode for contrast on white.
    assert "#0b5394" in about_html(theme.LIGHT)
    assert "#6ab0f3" in about_html(theme.DARK)


def test_dialog_builds_offscreen(qapp):
    dialog = AboutDialog()
    assert dialog.windowTitle() == f"About {APP_NAME}"
    dialog.deleteLater()


def test_help_menu_action_returns_dialog_without_exec(qapp):
    window = MainWindow()
    try:
        dialog = window.show_about()  # offscreen: no modal exec
        assert dialog.windowTitle() == f"About {APP_NAME}"
        assert window._about_action.isEnabled()  # never gated
    finally:
        window.close()


# --- the Updates section (UP3) ------------------------------------------
def test_status_says_nothing_it_has_not_proved():
    """A build that has not checked says so, rather than guessing either way."""
    assert update_status_text("0.11.0") == NOT_CHECKED


def test_status_while_checking():
    assert update_status_text("0.11.0", checking=True) == CHECKING


def test_status_reports_an_available_update():
    text = update_status_text("0.11.0", info=NEWER, checked=True)
    assert "99.0.0" in text
    assert "0.11.0" in text


def test_status_confirms_current_only_after_a_check():
    older = updates.UpdateInfo(version="0.0.1", page_url="https://example.com/old")
    text = update_status_text("0.11.0", info=older, checked=True)
    assert "latest version" in text
    assert "0.11.0" in text


def test_status_on_an_unreachable_feed_claims_nothing():
    """It says what happened. It must not imply an update does or doesn't exist."""
    text = update_status_text("0.11.0", error="no route to host", checked=True)
    assert "Couldn't reach" in text
    assert "latest version" not in text


def test_error_outranks_a_stale_earlier_result():
    """A failed re-check must not keep showing the previous answer as if fresh."""
    text = update_status_text("0.11.0", info=NEWER, error="timed out", checked=True)
    assert "Couldn't reach" in text


def test_dialog_starts_on_the_not_checked_state(qapp):
    dialog = AboutDialog()
    try:
        assert dialog.update_status() == NOT_CHECKED
        assert dialog.update_action() is None  # nothing to offer yet
    finally:
        dialog.deleteLater()


def test_dialog_shows_and_hides_the_action_button(qapp):
    dialog = AboutDialog()
    try:
        dialog.set_update_status("Version 99.0.0 is available.", action=UPDATE_NOW)
        assert dialog.update_action() == UPDATE_NOW
        dialog.set_update_status("You're on the latest version.", action=None)
        assert dialog.update_action() is None
    finally:
        dialog.deleteLater()


def test_check_button_is_disabled_while_a_check_runs(qapp):
    dialog = AboutDialog()
    try:
        dialog.set_update_status(CHECKING, busy=True)
        assert dialog._check_button.isEnabled() is False
        dialog.set_update_status(NOT_CHECKED, busy=False)
        assert dialog._check_button.isEnabled() is True
    finally:
        dialog.deleteLater()


def test_about_offers_the_in_place_upgrade_on_an_installed_build(qapp, monkeypatch):
    monkeypatch.setattr(mw.updates, "can_self_update", lambda *a: True)
    window = MainWindow()
    try:
        window._update_info = NEWER
        window._update_checked = True
        dialog = window.show_about()
        assert dialog.update_action() == UPDATE_NOW
        assert "99.0.0" in dialog.update_status()
    finally:
        window.close()


def test_about_offers_the_download_page_on_a_portable_build(qapp, monkeypatch):
    monkeypatch.setattr(mw.updates, "can_self_update", lambda *a: False)
    window = MainWindow()
    try:
        window._update_info = NEWER
        window._update_checked = True
        dialog = window.show_about()
        assert dialog.update_action() == DOWNLOAD_PAGE
    finally:
        window.close()


def test_about_offers_the_download_page_when_a_release_has_no_installer(qapp, monkeypatch):
    monkeypatch.setattr(mw.updates, "can_self_update", lambda *a: True)
    window = MainWindow()
    try:
        window._update_info = NO_INSTALLER
        window._update_checked = True
        dialog = window.show_about()
        assert dialog.update_action() == DOWNLOAD_PAGE
    finally:
        window.close()


def test_manual_check_ignores_a_skip_and_reports_the_truth(qapp, monkeypatch):
    """The banner stays quiet for a skipped version; a person who ASKED gets
    told about it anyway."""
    monkeypatch.setattr(mw.updates, "install_kind", lambda: updates.INSTALLED)
    monkeypatch.setattr(mw.updates, "can_self_update", lambda *a: True)
    monkeypatch.setattr(
        updates,
        "fetch_latest_release",
        lambda url=None: {
            "tag_name": "v99.0.0",
            "html_url": "https://example.com/v99",
            "assets": [],
        },
    )
    window = MainWindow()
    try:
        window._settings.set(updates.SKIPPED_VERSION_KEY, "99.0.0")
        dialog = window.show_about()
        window._update_checker.run_now()  # synchronous stand-in for the thread
        window._refresh_about(dialog)
        assert "99.0.0" in dialog.update_status()
        assert window._update_banner.info is None  # ...but the banner stays quiet
    finally:
        window.close()


def test_manual_check_ignores_the_daily_throttle(qapp, monkeypatch):
    monkeypatch.setattr(mw.updates, "install_kind", lambda: updates.INSTALLED)
    window = MainWindow()
    try:
        window._settings.set(updates.LAST_CHECK_KEY, updates.check_stamp())
        assert window.schedule_update_check() is False  # automatic: throttled
        started = []
        monkeypatch.setattr(
            window._update_checker, "start", lambda *a: started.append(True) or True
        )
        window._about_check(window.show_about())
        assert started == [True]  # manual: runs anyway
    finally:
        window.close()


def test_a_result_refreshes_an_open_about_dialog(qapp, monkeypatch):
    monkeypatch.setattr(mw.updates, "can_self_update", lambda *a: True)
    window = MainWindow()
    try:
        dialog = window.show_about()
        assert dialog.update_status() == NOT_CHECKED
        window._on_update_found(NEWER)  # arrives while the box is up
        assert "99.0.0" in dialog.update_status()
        assert dialog.update_action() == UPDATE_NOW
    finally:
        window.close()


def test_reopening_about_does_not_stack_checker_connections(qapp):
    """One reference, not a connection per open: connecting each time would
    leave a dead connection holding every closed dialog."""
    window = MainWindow()
    try:
        first = window.show_about()
        window._forget_about_dialog(first)  # offscreen stand-in for closing it
        second = window.show_about()
        assert window._about_dialog is second
        window._on_update_found(NEWER)
        # The live dialog updated; the closed one was not touched.
        assert "99.0.0" in second.update_status()
        assert first.update_status() == NOT_CHECKED
    finally:
        window.close()


def test_a_failed_recheck_still_offers_a_known_update(qapp, monkeypatch):
    """Deliberate: the status line reports the freshest fact (the check failed),
    but an update we already found is still real, so the way to get it stays."""
    monkeypatch.setattr(mw.updates, "can_self_update", lambda *a: True)
    window = MainWindow()
    try:
        window._on_update_found(NEWER)
        dialog = window.show_about()
        window._on_update_check_failed("timed out")
        assert "Couldn't reach" in dialog.update_status()
        assert dialog.update_action() == UPDATE_NOW
    finally:
        window.close()


def test_result_after_the_dialog_closed_does_not_crash(qapp):
    """The checker outlives the dialog, and its signals stay connected to it.
    A result arriving after the box was closed must not reach a deleted C++
    object — _refresh_about swallows exactly that RuntimeError."""
    window = MainWindow()
    try:
        dialog = window.show_about()
        dialog.deleteLater()
        qapp.processEvents()  # the C++ side is gone; the Python wrapper isn't
        window._refresh_about(dialog)  # must not raise
    finally:
        window.close()
