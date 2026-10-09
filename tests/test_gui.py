"""Optional desktop tests, including real windows controlling two daemon processes."""

import asyncio
import fcntl
import os
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
pytest.importorskip("pysync_client_gui")

from conftest import OWNER
from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox
from pysync_client_cli.config import load_config
from pysync_client_cli.database import Database
from pysync_client_gui.backend import Backend
from pysync_client_gui.dialogs import AddProjectDialog
from pysync_client_gui.theme import apply_theme
from pysync_client_gui.window import MainWindow


@pytest.fixture(scope="session")
def qt_app():
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    apply_theme(application)
    return application


@pytest.fixture
def window(qt_app, tmp_path):
    result = MainWindow(Backend(tmp_path / "gui-config"), poll=False)
    result.show()
    yield result
    result.close()
    qt_app.processEvents()


async def pump(application, predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        application.processEvents()
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Desktop operation timed out")


async def refresh(application, window):
    window.refresh()
    await pump(application, lambda w=window: not w.jobs)


async def test_unconfigured_gui_shows_onboarding_without_creating_state(qt_app, window):
    await refresh(qt_app, window)
    assert "Začněte" in window.welcome_title.text()
    assert not window.service_button.isEnabled()
    assert window.save_button.isEnabled()
    assert window.owner_token.echoMode() == QLineEdit.EchoMode.Password
    assert not window.backend.directory.exists()
    window.welcome_action.click()
    assert window.pages.currentIndex() == 4


async def test_refresh_preserves_unsaved_form_and_validation_preserves_input(qt_app, window):
    await refresh(qt_app, window)
    window.server_url.setText("not a URL")
    window.device_name.setText("Moje zařízení")
    window.owner_token.setText("private input")
    await refresh(qt_app, window)
    assert window.server_url.text() == "not a URL"
    window.save_button.click()
    assert window.settings_error.text()
    assert window.device_name.text() == "Moje zařízení"
    assert window.owner_token.text() == "private input"
    assert not window.backend.directory.exists()


async def test_worker_is_responsive_delivers_on_main_thread_and_deduplicates(qt_app, window):
    gate = threading.Event()
    received = []
    main_thread = threading.get_ident()

    def operation():
        gate.wait(2)
        return threading.get_ident()

    def completed(worker_thread):
        received.append((worker_thread, threading.get_ident()))

    try:
        assert window.submit("test", operation, completed)
        assert not window.submit("test", operation, completed)
        window.navigate(4)
        qt_app.processEvents()
        assert window.pages.currentIndex() == 4
        assert not received
    finally:
        gate.set()
        await pump(qt_app, lambda w=window: not w.jobs)
    assert len(received) == 1
    assert received[0][0] != main_thread
    assert received[0][1] == main_thread


async def test_worker_error_and_close_while_request_is_running(qt_app, window):
    gate = threading.Event()

    def failure():
        gate.wait(2)
        raise ValueError("Cannot read project")

    window.submit("test-error", failure, lambda _: None)
    gate.set()
    await pump(qt_app, lambda w=window: not w.jobs)
    assert window.notice_text.text() == "Cannot read project"
    assert window.retry.isVisible()
    gate.clear()
    window.submit("closing", lambda: gate.wait(2), lambda _: None)
    window.close()
    assert window.closing and window.isVisible()
    gate.set()
    await pump(qt_app, lambda w=window: not w.jobs and not window.isVisible())


async def test_status_controls_pending_detach_and_reconnection(qt_app, window, monkeypatch):
    await refresh(qt_app, window)
    status = window.status_data
    status["preferences"]["device_id"] = "registered"
    status["running"] = True
    status["error"] = "connection refused"
    status["projects"] = [
        {
            "id": "p",
            "name": "Project",
            "root": "/tmp/project",
            "cursor": 3,
            "initialized": True,
            "pending": 2,
            "last_sync": None,
            "error": None,
        }
    ]
    window.render_status(status)
    window.projects_table.selectRow(0)
    assert window.pending_stat.value.text() == "2"
    assert not window.save_button.isEnabled()
    assert not window.server_url.isEnabled()
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_: pytest.fail("Must refuse pending detach")
    )
    window.remove_project()
    assert "čekající změny" in window.notice_text.text()
    # A connection error clears when the server reconnects, without changing table selection.
    window.render_status(status)
    status.update(connected=True, error=None)
    window.render_status(status)
    assert not window.notice.isVisible()
    assert window.selected_project()["id"] == "p"
    status["projects"] = []
    window.render_status(status)
    assert window.selected_project() is None
    assert not window.detach.isEnabled()


async def test_stopped_status_is_read_only_and_keeps_persistent_diagnostics(tmp_path):
    backend = Backend(tmp_path / "config")
    database = Database(backend.directory)
    with database.connection:
        database.connection.execute(
            "INSERT INTO projects(id,name,root,cursor,initialized) VALUES('p','Offline','/tmp/offline',8,1)"
        )
        database.connection.execute(
            "INSERT INTO pending(id,project_id,path,metadata) VALUES('op','p','file.py','{}')"
        )
        database.record_conflict("p", {"id": "c", "path": "file.py", "conflict_path": "/tmp/copy"})
    database.close()
    before = (backend.directory / "state.sqlite").read_bytes()
    status = await backend.status()
    assert not status["running"] and not status["connected"]
    assert status["projects"][0]["pending"] == 1
    assert status["projects"][0]["cursor"] == 8
    assert status["conflicts"][0]["id"] == "c"
    assert (backend.directory / "state.sqlite").read_bytes() == before


async def test_configuration_requires_daemon_lock_and_invalid_token_does_not_save(server, tmp_path):
    backend = Backend(tmp_path / "config")
    values = {"server_url": server.url, "device_name": "desktop"}
    with pytest.raises(httpx.HTTPStatusError, match="401"):
        await backend.configure(values, "wrong-owner-token-more-than-24-chars")
    assert not (backend.directory / "config.json").exists()
    with (backend.directory / "daemon.lock").open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="zastavte"):
            await backend.configure(values, OWNER)
    assert not (backend.directory / "config.json").exists()


def test_project_plan_requires_review_and_invalidates_on_edit(qt_app, window, tmp_path):
    calls = []

    def submit(key, operation, success, failure):
        calls.append((key, operation, success, failure))
        return True

    dialog = AddProjectDialog(window.backend, submit, window)
    dialog.loaded({"projects": [{"id": "server-id", "name": "Existing"}]})
    dialog.root.setText(str(tmp_path))
    dialog.name.setText("Example")
    dialog.preview.click()
    assert calls[-1][0] == "dialog-plan"
    assert not dialog.browse_button.isEnabled()
    assert not dialog.apply.isEnabled()
    dialog.show_plan(
        {
            "project_id": "server-id",
            "local_files": 2,
            "downloads": 1,
            "local_only": 1,
            "collisions": ["main.py"],
            "accept_local": False,
        }
    )
    assert "main.py" in dialog.review.toPlainText()
    assert dialog.apply.isEnabled()
    dialog.accept_local.setChecked(True)
    assert not dialog.apply.isEnabled()
    assert dialog.plan_arguments is None
    dialog.projects_failed("offline")
    dialog.close()


async def test_gui_controls_two_real_daemons_and_preserves_initial_local_content(
    qt_app, server, monkeypatch
):
    with tempfile.TemporaryDirectory(prefix="pysync-gui-") as temp:
        workspace = Path(temp).resolve()
        windows, roots = [], []
        try:
            for index in range(2):
                backend = Backend(workspace / f"state-{index}")
                window = MainWindow(backend, poll=False)
                windows.append(window)
                window.show()
                await refresh(qt_app, window)
                window.navigate(4)
                window.server_url.setText(server.url)
                window.device_name.setText(f"gui-{index}")
                window.owner_token.setText(OWNER)
                window.poll_seconds.setValue(0.2)
                window.debounce_ms.setValue(100)
                window.save_button.click()
                await pump(qt_app, lambda w=window: not w.jobs)
                assert load_config(backend.directory).device_id, window.notice_text.text()
                assert OWNER not in (backend.directory / "config.json").read_text()
                assert (backend.directory / "config.json").stat().st_mode & 0o077 == 0
                assert not window.owner_token.text()
                window.service_button.click()
                await pump(qt_app, lambda w=window: not w.jobs)
                assert window.status_data["running"], window.notice_text.text()
                root = workspace / f"files-{index}"
                roots.append(root)
                root.mkdir()
                (root / "initial.py").write_text(
                    "accepted server content" if index == 0 else "preexisting local content"
                )
                window.open_add()
                await pump(qt_app, lambda w=window: not w.jobs)
                dialog = window.add_dialog
                dialog.root.setText(str(root))
                dialog.name.setText("GUI project")
                dialog.accept_local.setChecked(index == 0)
                dialog.preview.click()
                await pump(qt_app, lambda w=window: not w.jobs)
                assert dialog.apply.isEnabled(), dialog.error.text()
                assert not (await backend.status())["projects"]  # Review has not attached locally.
                dialog.apply.click()
                await pump(qt_app, lambda w=window: not w.jobs)
                assert not dialog.isVisible(), dialog.error.text()
                await pump(
                    qt_app,
                    lambda r=root: (r / "initial.py").read_text() == "accepted server content",
                )

            copies = list((roots[1] / ".pysync-recovery").rglob("*"))
            assert any(p.is_file() and p.read_text() == "preexisting local content" for p in copies)
            source = roots[0] / "složka s mezerou" / "žluťoučký.py"
            source.parent.mkdir()
            source.write_text("created using the GUI-managed daemon")
            target = roots[1] / source.relative_to(roots[0])
            await pump(qt_app, lambda: target.exists() and target.read_text() == source.read_text())
            source.write_text("modified with desktop service running")
            await pump(qt_app, lambda: target.read_text() == source.read_text())
            moved = source.with_name("přejmenovaný.py")
            source.rename(moved)
            remote_moved = target.with_name(moved.name)
            await pump(qt_app, lambda: remote_moved.exists() and not target.exists())
            moved.unlink()
            await pump(qt_app, lambda: not remote_moved.exists())
            await refresh(qt_app, windows[1])
            assert windows[1].conflicts_table.rowCount() == 1
            assert windows[1].projects_table.rowCount() == 1
            windows[1].navigate(2)
            windows[1].conflicts_table.selectRow(0)
            monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)
            windows[1].resolve_button.click()
            await pump(qt_app, lambda: not windows[1].jobs)
            assert windows[1].conflicts_table.rowCount() == 0
            assert any(p.is_file() and p.read_text() == "preexisting local content" for p in copies)
            windows[1].navigate(3)
            await pump(qt_app, lambda: not windows[1].jobs)
            assert windows[1].log_text.toPlainText()
            # Pausing and reopening the UI retains identity, history and queued-state diagnostics.
            windows[0].service_button.click()
            await pump(qt_app, lambda: not windows[0].jobs)
            assert not windows[0].status_data["running"]
            windows[0].service_button.click()
            await pump(qt_app, lambda: not windows[0].jobs)
            assert windows[0].status_data["running"]
        finally:
            for window in windows:
                await pump(qt_app, lambda w=window: not w.jobs)
                try:
                    await window.backend.stop()
                except ValueError:
                    pass
                window.close()
            qt_app.processEvents()
            await asyncio.sleep(0.2)
