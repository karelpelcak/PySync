import socket
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer, QUrl, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from pysync_client_cli.config import Config
from pysync_shared.paths import normalize_path

from pysync_client_gui.backend import Backend
from pysync_client_gui.dialogs import AddProjectDialog
from pysync_client_gui.widgets import StatCard, button, label, table
from pysync_client_gui.workers import Worker

TITLES = [
    ("Přehled synchronizace", "Vaše rozpracovaná práce, vždy po ruce."),
    ("Vaše projekty", "Složky na tomto zařízení propojené přes váš server."),
    ("Konflikty", "Obě verze jsou zachované. Vy rozhodnete, jak je sloučit."),
    ("Aktivita a logy", "Co se děje na tomto zařízení."),
    ("Nastavení zařízení", "Připojení k serveru a pravidla synchronizace."),
]


class MainWindow(QMainWindow):
    def __init__(self, backend: Backend, poll: bool = True):
        super().__init__()
        self.backend = backend
        self.jobs: dict[str, Worker] = {}
        self.callbacks: dict[str, tuple[Callable, Callable | None]] = {}
        self.status_data: dict[str, Any] = {}
        self.form_loaded = False
        self.closing = False
        self.connection_notice = False
        self.setWindowTitle("PySync")
        self.resize(1190, 810)
        self.setMinimumSize(920, 680)
        workspace = QWidget()
        workspace.setObjectName("workspace")
        outer = QHBoxLayout(workspace)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.setCentralWidget(workspace)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(216)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(20, 32, 20, 23)
        side.setSpacing(8)
        side.addWidget(label("⇄  PySync", "brand"))
        side.addWidget(label("VAŠE PRÁCE V POHYBU", "eyebrow"))
        side.addSpacing(36)
        self.navigation = []
        group = QButtonGroup(self)
        for index, text in enumerate(["Přehled", "Projekty", "Konflikty", "Logy", "Nastavení"]):
            nav = button(text)
            nav.setProperty("nav", True)
            nav.setCheckable(True)
            nav.setAccessibleName(text)
            group.addButton(nav, index)
            self.navigation.append(nav)
            side.addWidget(nav)
        group.idClicked.connect(self.navigate)
        self.navigation[0].setChecked(True)
        side.addStretch()
        self.service_state = label("Načítám stav služby…", "subtitle", True)
        side.addWidget(self.service_state)
        self.service_button = button("Spustit synchronizaci", True)
        self.service_button.setEnabled(False)
        self.service_button.clicked.connect(self.toggle_service)
        side.addWidget(self.service_button)
        side.addSpacing(12)
        side.addWidget(
            label(
                "Soubory putují přes váš server.\nPřímo mezi zařízeními se nespojují.",
                "subtitle",
                True,
            )
        )
        outer.addWidget(sidebar)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(30, 25, 30, 20)
        body.setSpacing(19)
        top = QHBoxLayout()
        self.device_label = label("Načítám zařízení…", "subtitle")
        top.addWidget(self.device_label, 1)
        self.connectivity = label("Zjišťuji připojení", "pill")
        top.addWidget(self.connectivity)
        self.refresh_button = button("Obnovit")
        self.refresh_button.clicked.connect(self.refresh)
        top.addWidget(self.refresh_button)
        body.addLayout(top)
        self.notice = QFrame()
        self.notice.setObjectName("notice")
        notice_row = QHBoxLayout(self.notice)
        self.notice_text = label("", wrap=True)
        self.notice_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        notice_row.addWidget(self.notice_text, 1)
        self.retry = button("Zkusit znovu")
        self.retry.clicked.connect(self.refresh)
        notice_row.addWidget(self.retry)
        self.notice.hide()
        body.addWidget(self.notice)
        self.title = label(TITLES[0][0], "title")
        self.subtitle = label(TITLES[0][1], "subtitle", True)
        body.addWidget(self.title)
        body.addWidget(self.subtitle)
        self.pages = QStackedWidget()
        body.addWidget(self.pages, 1)
        outer.addWidget(content, 1)
        self.build_overview()
        self.build_projects()
        self.build_conflicts()
        self.build_logs()
        self.build_settings()
        self.busy_label = label("", "subtitle")
        body.addWidget(self.busy_label)
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.refresh)
        if poll:
            self.timer.start()
            QTimer.singleShot(0, self.refresh)

    def page(self) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)
        self.pages.addWidget(page)
        return page, layout

    def build_overview(self) -> None:
        _page, layout = self.page()
        stats = QHBoxLayout()
        self.project_stat = StatCard("Projekty", "Propojené složky")
        self.pending_stat = StatCard("Čekající změny", "Bezpečně uložené na tomto zařízení")
        self.conflict_stat = StatCard("Konflikty", "Obě verze zůstávají zachované")
        for card in (self.project_stat, self.pending_stat, self.conflict_stat):
            stats.addWidget(card, 1)
        layout.addLayout(stats)
        self.welcome = QFrame()
        self.welcome.setProperty("role", "card")
        welcome = QVBoxLayout(self.welcome)
        welcome.setContentsMargins(23, 20, 23, 20)
        self.welcome_title = label("Načítám stav synchronizace…", "section")
        self.welcome_detail = label("", "subtitle", True)
        welcome.addWidget(self.welcome_title)
        welcome.addWidget(self.welcome_detail)
        self.welcome_action = button("Připojit zařízení", True)
        self.welcome_action.clicked.connect(self.onboarding)
        welcome.addWidget(self.welcome_action, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.welcome)
        actions = QHBoxLayout()
        actions.addWidget(label("Projekty na tomto zařízení", "section"), 1)
        self.sync_button = button("Synchronizovat teď")
        self.sync_button.clicked.connect(self.sync_all)
        actions.addWidget(self.sync_button)
        self.add_button = button("+ Připojit projekt", True)
        self.add_button.clicked.connect(self.open_add)
        actions.addWidget(self.add_button)
        layout.addLayout(actions)
        self.overview_table = table(["Projekt", "Stav", "Čekající", "Poslední synchronizace"])
        self.overview_table.doubleClicked.connect(lambda _index: self.navigate(1))
        layout.addWidget(self.overview_table, 1)
        self.transfer_text = label("Žádný přenos neprobíhá.", "subtitle")
        layout.addWidget(self.transfer_text)
        self.transfer_progress = QProgressBar()
        self.transfer_progress.setRange(0, 100)
        self.transfer_progress.setValue(0)
        self.transfer_progress.setTextVisible(False)
        self.transfer_progress.setAccessibleName("Průběh přenosu souboru")
        layout.addWidget(self.transfer_progress)

    def build_projects(self) -> None:
        _page, layout = self.page()
        row = QHBoxLayout()
        self.project_empty = label("Načítám projekty…", "subtitle", True)
        row.addWidget(self.project_empty, 1)
        add = button("+ Připojit projekt", True)
        add.clicked.connect(self.open_add)
        row.addWidget(add)
        self.project_add = add
        layout.addLayout(row)
        self.projects_table = table(["Projekt", "Lokální složka", "Stav", "Čekající"])
        self.projects_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.projects_table.itemSelectionChanged.connect(self.controls)
        self.projects_table.doubleClicked.connect(lambda _index: self.open_project())
        layout.addWidget(self.projects_table, 1)
        self.project_detail = label("Vyberte projekt pro zobrazení jeho stavu.", "subtitle", True)
        self.project_detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.project_detail)
        actions = QHBoxLayout()
        self.open_folder = button("Otevřít složku")
        self.open_folder.clicked.connect(self.open_project)
        self.sync_selected = button("Synchronizovat projekt")
        self.sync_selected.clicked.connect(self.sync_project)
        self.detach = button("Odpojit projekt")
        self.detach.clicked.connect(self.remove_project)
        for widget in (self.open_folder, self.sync_selected, self.detach):
            actions.addWidget(widget)
        actions.addStretch()
        layout.addLayout(actions)

    def build_conflicts(self) -> None:
        _page, layout = self.page()
        self.conflicts_empty = label("Načítám konflikty…", "subtitle", True)
        layout.addWidget(self.conflicts_empty)
        self.conflicts_table = table(["Soubor", "Zachovaná kopie", "Typ"])
        self.conflicts_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.conflicts_table.itemSelectionChanged.connect(self.controls)
        layout.addWidget(self.conflicts_table, 1)
        layout.addWidget(
            label(
                "Sloučte požadovaný obsah do původního souboru, nechte ho synchronizovat a poté označte konflikt za vyřešený. Žádná kopie se tím nesmaže.",
                "subtitle",
                True,
            )
        )
        row = QHBoxLayout()
        self.open_conflict_button = button("Otevřít umístění kopie")
        self.open_conflict_button.clicked.connect(self.open_conflict)
        self.resolve_button = button("Označit jako vyřešený")
        self.resolve_button.clicked.connect(self.resolve_conflict)
        row.addWidget(self.open_conflict_button)
        row.addWidget(self.resolve_button)
        row.addStretch()
        layout.addLayout(row)

    def build_logs(self) -> None:
        _page, layout = self.page()
        row = QHBoxLayout()
        row.addWidget(label("Posledních 200 záznamů synchronizační služby", "subtitle"), 1)
        refresh = button("Obnovit logy")
        refresh.clicked.connect(self.refresh_logs)
        row.addWidget(refresh)
        layout.addLayout(row)
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setAccessibleName("Logy synchronizační služby")
        self.log_text.setPlaceholderText("Logy načteme při otevření této stránky.")
        layout.addWidget(self.log_text, 1)

    def build_settings(self) -> None:
        page, layout = self.page()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        panel = QWidget()
        form_layout = QVBoxLayout(panel)
        form_layout.setContentsMargins(0, 0, 12, 10)
        form_layout.setSpacing(14)
        self.identity = label("Připojte zařízení ke svému PySync serveru.", "subtitle", True)
        self.identity.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form_layout.addWidget(self.identity)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setSpacing(8)
        self.server_url = QLineEdit("http://127.0.0.1:8000")
        self.server_url.setPlaceholderText("https://raspberrypi.vaše-síť.ts.net")
        form.addRow("&Adresa serveru", self.server_url)
        self.device_name = QLineEdit(socket.gethostname().split(".")[0])
        self.device_name.setMaxLength(80)
        form.addRow("&Název tohoto zařízení", self.device_name)
        self.owner_label = label("Token &vlastníka serveru")
        self.owner_token = QLineEdit()
        self.owner_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.owner_token.setMaxLength(4096)
        self.owner_token.setPlaceholderText("Pouze pro první registraci; neukládá se.")
        self.owner_label.setBuddy(self.owner_token)
        form.addRow(self.owner_label, self.owner_token)
        form_layout.addLayout(form)
        self.show_token = QCheckBox("Zobrazit zadaný token")
        self.show_token.toggled.connect(
            lambda checked: self.owner_token.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        form_layout.addWidget(self.show_token)
        self.insecure = QCheckBox("Povolit HTTP v důvěryhodné síti nebo VPN")
        form_layout.addWidget(self.insecure)
        form_layout.addWidget(
            label(
                "Pro vzdálený přístup použijte HTTPS. Lokální http://127.0.0.1 funguje bez této volby.",
                "subtitle",
                True,
            )
        )
        form_layout.addWidget(label("Pravidla synchronizace", "section"))
        options = QFormLayout()
        self.poll_seconds = QDoubleSpinBox()
        self.poll_seconds.setRange(0.1, 300)
        self.poll_seconds.setDecimals(1)
        self.poll_seconds.setSuffix(" s")
        options.addRow("Interval úplné &kontroly", self.poll_seconds)
        self.debounce_ms = QSpinBox()
        self.debounce_ms.setRange(50, 30000)
        self.debounce_ms.setSuffix(" ms")
        options.addRow("Čekání po &změně souboru", self.debounce_ms)
        self.max_size = QSpinBox()
        self.max_size.setRange(1, 10240)
        self.max_size.setSuffix(" MiB")
        options.addRow("Maximální &velikost souboru", self.max_size)
        form_layout.addLayout(options)
        self.global_ignore = QPlainTextEdit()
        self.global_ignore.setMaximumHeight(115)
        self.global_ignore.setAccessibleName("Globální ignorované soubory, jeden vzor na řádek")
        form_layout.addWidget(
            label("Globální ignorované soubory — jeden vzor na řádek", "subtitle")
        )
        form_layout.addWidget(self.global_ignore)
        self.settings_error = label("", wrap=True)
        self.settings_error.setStyleSheet("color: #ffaeb5;")
        form_layout.addWidget(self.settings_error)
        self.save_button = button("Připojit zařízení", True)
        self.save_button.clicked.connect(self.save_settings)
        form_layout.addWidget(self.save_button, alignment=Qt.AlignmentFlag.AlignLeft)
        form_layout.addWidget(
            label(f"Konfigurace a stav: {self.backend.directory}", "subtitle", True)
        )
        form_layout.addStretch()
        scroll.setWidget(panel)
        layout.addWidget(scroll)
        page.setAccessibleName("Nastavení PySync")

    def submit(
        self, key: str, operation: Callable, success: Callable, failure: Callable | None = None
    ) -> bool:
        if self.closing or key in self.jobs:
            return False
        worker = Worker(key, operation, self)
        self.jobs[key] = worker
        self.callbacks[key] = (success, failure)
        worker.completed.connect(self.job_succeeded)
        worker.failed.connect(self.job_failed)
        worker.finished.connect(self.job_finished)
        self.controls()
        worker.start()
        return True

    @Slot(str, object)
    def job_succeeded(self, key: str, result: Any) -> None:
        callback = self.callbacks.pop(key, None)
        if callback and not self.closing:
            callback[0](result)

    @Slot(str, str)
    def job_failed(self, key: str, message: str) -> None:
        callback = self.callbacks.pop(key, None)
        if self.closing:
            return
        if callback and callback[1]:
            callback[1](message)
        else:
            self.notify(message, error=True)

    @Slot()
    def job_finished(self) -> None:
        worker = self.sender()
        self.jobs.pop(worker.key, None)
        self.callbacks.pop(worker.key, None)
        worker.deleteLater()
        self.controls()
        if self.closing and not self.jobs:
            self.close()

    def refresh(self) -> None:
        self.submit("status", self.backend.status, self.render_status)
        if self.pages.currentIndex() == 3:
            self.refresh_logs()

    def refresh_logs(self) -> None:
        self.submit("logs", self.backend.logs, self.log_text.setPlainText)

    @Slot(int)
    def navigate(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        self.navigation[index].setChecked(True)
        self.title.setText(TITLES[index][0])
        self.subtitle.setText(TITLES[index][1])
        if index == 3:
            self.refresh_logs()

    def notify(self, text: str, error: bool = False) -> None:
        self.connection_notice = False
        self.notice_text.setText(text)
        self.notice.setProperty("error", error)
        self.notice.style().unpolish(self.notice)
        self.notice.style().polish(self.notice)
        self.retry.setVisible(error)
        self.notice.setVisible(bool(text))

    def selected_project(self) -> dict | None:
        row = self.projects_table.currentRow()
        if row < 0 or not self.projects_table.item(row, 0):
            return None
        pid = self.projects_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        return next((p for p in self.status_data.get("projects", []) if p["id"] == pid), None)

    def selected_conflict(self) -> dict | None:
        row = self.conflicts_table.currentRow()
        if row < 0 or not self.conflicts_table.item(row, 0):
            return None
        cid = self.conflicts_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        return next((c for c in self.status_data.get("conflicts", []) if c["id"] == cid), None)

    def controls(self) -> None:
        running = self.status_data.get("running", False)
        registered = bool(self.status_data.get("preferences", {}).get("device_id"))
        busy = any(key not in ("status", "logs") for key in self.jobs)
        selected = self.selected_project()
        conflict = self.selected_conflict()
        self.service_button.setText(
            "Pozastavit synchronizaci" if running else "Spustit synchronizaci"
        )
        self.service_button.setEnabled(registered and not busy)
        for widget in (self.sync_button, self.add_button, self.project_add):
            widget.setEnabled(running and not busy)
        self.open_folder.setEnabled(selected is not None)
        self.sync_selected.setEnabled(running and selected is not None and not busy)
        self.detach.setEnabled(running and selected is not None and not busy)
        self.resolve_button.setEnabled(running and conflict is not None and not busy)
        self.open_conflict_button.setEnabled(conflict is not None)
        self.save_button.setEnabled(not busy and not running)
        self.save_button.setToolTip("Nejprve pozastavte synchronizaci." if running else "")
        self.welcome_action.setEnabled(not busy)
        for field in (
            self.insecure,
            self.poll_seconds,
            self.debounce_ms,
            self.max_size,
            self.global_ignore,
            self.owner_token,
            self.show_token,
        ):
            field.setEnabled(not running and "settings" not in self.jobs)
        self.device_name.setEnabled(not registered and not running and "settings" not in self.jobs)
        self.server_url.setEnabled(not registered and not running and "settings" not in self.jobs)
        self.server_url.setToolTip(
            "Registrované zařízení je vázané na tento server." if registered else ""
        )
        self.busy_label.setText(
            "Dokončuji probíhající operaci…" if self.closing else "Probíhá operace…" if busy else ""
        )
        if selected:
            last = selected.get("last_sync")
            self.project_detail.setText(
                f"{selected['root']}\nRevize {selected['cursor']} · Poslední synchronizace: "
                f"{self.time_text(last)}\n" + self.project_error(selected)
            )
        else:
            self.project_detail.setText("Vyberte projekt pro zobrazení jeho stavu.")

    @staticmethod
    def time_text(timestamp: float | None) -> str:
        return (
            datetime.fromtimestamp(timestamp).strftime("%d. %m. %H:%M:%S")
            if timestamp
            else "Zatím neproběhla"
        )

    @staticmethod
    def project_error(project: dict) -> str:
        return project.get("error") or "; ".join(
            e["message"] for e in project.get("scan_errors", [])
        )

    def project_state(self, project: dict) -> str:
        if self.project_error(project):
            return "Vyžaduje pozornost"
        if not self.status_data.get("running"):
            return "Pozastaveno"
        if not project["initialized"]:
            return "První synchronizace"
        if project["pending"]:
            return "Čekající změny"
        if not self.status_data.get("connected"):
            return "Čeká na připojení"
        return "Synchronizováno"

    def fill_table(self, widget, rows: list[list[str]], ids: list[str]) -> None:
        selected = None
        if widget.currentRow() >= 0 and widget.item(widget.currentRow(), 0):
            selected = widget.item(widget.currentRow(), 0).data(Qt.ItemDataRole.UserRole)
        widget.blockSignals(True)
        widget.clearSelection()
        widget.setCurrentCell(-1, -1)
        widget.setRowCount(len(rows))
        for index, values in enumerate(rows):
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, ids[index])
                widget.setItem(index, column, item)
            if ids[index] == selected:
                widget.selectRow(index)
        widget.blockSignals(False)

    def render_status(self, status: dict) -> None:
        self.status_data = status
        prefs = status["preferences"]
        projects = status["projects"]
        conflicts = status["conflicts"]
        registered = bool(prefs["device_id"])
        self.device_label.setText(
            f"{prefs['device_name'] or 'Toto zařízení'}  ·  {prefs['server_url']}"
        )
        self.connectivity.setText(
            "Server připojen"
            if status["connected"]
            else "Synchronizace pozastavena"
            if registered and not status["running"]
            else "Server nedostupný"
            if registered
            else "Zařízení není připojené"
        )
        self.connectivity.setProperty("online", bool(status["connected"]))
        self.connectivity.style().unpolish(self.connectivity)
        self.connectivity.style().polish(self.connectivity)
        self.service_state.setText(
            "Služba běží na pozadí" if status["running"] else "Služba je zastavená"
        )
        self.project_stat.value.setText(str(len(projects)))
        self.pending_stat.value.setText(str(sum(p["pending"] for p in projects)))
        self.conflict_stat.value.setText(str(len(conflicts)))
        self.welcome.setVisible(not registered or not projects or not status["running"])
        if not registered:
            self.welcome_title.setText("Začněte připojením zařízení")
            self.welcome_detail.setText(
                "Zadejte adresu svého serveru a zaregistrujte toto zařízení. Potom připojte složku s projektem."
            )
            self.welcome_action.setText("Připojit zařízení")
        elif not status["running"]:
            self.welcome_title.setText("Synchronizace je pozastavená")
            self.welcome_detail.setText(
                "Vaše soubory i čekající změny zůstávají uložené. Spusťte službu a pokračujte v synchronizaci."
            )
            self.welcome_action.setText("Spustit synchronizaci")
        else:
            self.welcome_title.setText("Připojte svůj první projekt")
            self.welcome_detail.setText(
                "Vyberte složku na tomto zařízení. Před synchronizací zobrazíme plán změn."
            )
            self.welcome_action.setText("+ Připojit projekt")
        self.welcome_action.setEnabled(not any(k not in ("status", "logs") for k in self.jobs))
        self.project_empty.setText(
            f"{len(projects)} propojených projektů"
            if projects
            else "Zatím nemáte žádný projekt. Připojte existující složku nebo prázdnou složku pro stažení ze serveru."
        )
        self.fill_table(
            self.overview_table,
            [
                [
                    p["name"],
                    self.project_state(p),
                    str(p["pending"]),
                    self.time_text(p.get("last_sync")),
                ]
                for p in projects
            ],
            [p["id"] for p in projects],
        )
        self.fill_table(
            self.projects_table,
            [
                [
                    p["name"],
                    p["root"],
                    self.project_state(p),
                    str(p["pending"])
                    + (f" ({p['paused_pending']} ignorovaných)" if p.get("paused_pending") else ""),
                ]
                for p in projects
            ],
            [p["id"] for p in projects],
        )
        self.conflicts_empty.setText(
            f"{len(conflicts)} konfliktů čeká na kontrolu."
            if conflicts
            else "Žádné nevyřešené konflikty. Pokud vznikne rozdíl, obě verze zde najdete."
        )
        kinds = {
            "initial_local_preserved": "První synchronizace",
            "interrupted_application_preserved": "Přerušený zápis",
            "concurrent_local_save_preserved": "Souběžný lokální zápis",
        }
        self.fill_table(
            self.conflicts_table,
            [
                [
                    c["path"],
                    c.get("conflict_path") or "Novější serverový soubor zůstal zachován",
                    kinds.get(c.get("kind"), "Souběžné změny"),
                ]
                for c in conflicts
            ],
            [c["id"] for c in conflicts],
        )
        transfer = status.get("transfer")
        if transfer:
            direction = "Nahrávám" if transfer["direction"] == "upload" else "Stahuji"
            self.transfer_text.setText(
                f"{direction}: {transfer['path']} · {transfer['bytes']:,} / {transfer['total']:,} B"
            )
            self.transfer_progress.setValue(
                int(100 * transfer["bytes"] / max(1, transfer["total"]))
            )
        else:
            self.transfer_text.setText("Žádný přenos neprobíhá.")
            self.transfer_progress.setValue(0)
        self.identity.setText(
            f"Registrované zařízení: {prefs['device_name']}\nID: {prefs['device_id']}"
            if registered
            else "Token vlastníka použijeme pouze k registraci. Přihlašovací údaje zařízení uložíme do soukromé konfigurace."
        )
        self.owner_label.setVisible(not registered)
        self.owner_token.setVisible(not registered)
        self.show_token.setVisible(not registered)
        self.save_button.setText("Uložit nastavení" if registered else "Připojit zařízení")
        if not self.form_loaded:
            self.populate_preferences(prefs)
        self.controls()
        if status.get("error") and status["running"]:
            self.notify(
                f"Server není dostupný. Lokální změny zůstanou ve frontě. {status['error']}",
                error=True,
            )
            self.connection_notice = True
        elif self.connection_notice:
            self.notify("")

    def populate_preferences(self, prefs: dict) -> None:
        self.server_url.setText(prefs["server_url"])
        self.device_name.setText(prefs["device_name"] or socket.gethostname().split(".")[0])
        self.insecure.setChecked(prefs["allow_insecure"])
        self.poll_seconds.setValue(prefs["poll_seconds"])
        self.debounce_ms.setValue(prefs["debounce_ms"])
        self.max_size.setValue(max(1, prefs["max_file_size"] // (1024 * 1024)))
        self.global_ignore.setPlainText("\n".join(prefs["ignore"]))
        self.form_loaded = True

    def onboarding(self) -> None:
        if not self.status_data.get("preferences", {}).get("device_id"):
            self.navigate(4)
            self.server_url.setFocus()
        elif not self.status_data.get("running"):
            self.toggle_service()
        else:
            self.open_add()

    def toggle_service(self) -> None:
        operation = self.backend.stop if self.status_data.get("running") else self.backend.start
        self.submit("action", operation, self.service_changed)

    def service_changed(self, status: dict) -> None:
        self.render_status(status)
        self.notify(
            "Synchronizace běží na pozadí. Zavření okna ji nezastaví."
            if status["running"]
            else "Synchronizace je pozastavená; soubory i fronta zůstávají uložené."
        )

    def sync_all(self) -> None:
        self.submit("action", self.backend.sync, self.render_status)

    def sync_project(self) -> None:
        project = self.selected_project()
        if project:
            self.submit("action", lambda: self.backend.sync(project["name"]), self.render_status)

    def open_add(self) -> None:
        if not self.status_data.get("running"):
            self.notify("Nejprve spusťte synchronizaci tlačítkem vlevo.", error=True)
            return
        dialog = AddProjectDialog(self.backend, self.submit, self)
        dialog.accepted.connect(self.project_added)
        self.add_dialog = dialog
        dialog.show()
        dialog.load_projects()

    def project_added(self) -> None:
        self.notify("Projekt je připojený. První synchronizace probíhá na pozadí.")
        self.refresh()

    def open_project(self) -> None:
        project = self.selected_project()
        if project:
            QDesktopServices.openUrl(QUrl.fromLocalFile(project["root"]))

    def remove_project(self) -> None:
        project = self.selected_project()
        if not project:
            return
        if project["pending"]:
            self.notify(
                "Projekt má čekající změny. Nechte je nejprve synchronizovat; GUI je nezahodí.",
                error=True,
            )
            return
        answer = QMessageBox.question(
            self,
            "Odpojit projekt?",
            f"Odpojit projekt {project['name']} z tohoto zařízení?\nLokální soubory a historie na serveru zůstanou zachované.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.submit(
                "action", lambda: self.backend.remove(project["name"]), self.project_removed
            )

    def project_removed(self, _result: dict) -> None:
        self.notify("Projekt je odpojený. Lokální soubory zůstaly zachované.")
        self.refresh()

    def open_conflict(self) -> None:
        conflict = self.selected_conflict()
        if not conflict:
            return
        path = conflict.get("conflict_path")
        if path and Path(path).is_absolute() and conflict.get("kind"):
            target = Path(path)
        else:
            project = next(
                (p for p in self.status_data["projects"] if p["id"] == conflict.get("project_id")),
                None,
            )
            if not project:
                self.notify("Lokální složku tohoto konfliktu se nepodařilo najít.", error=True)
                return
            target = Path(project["root"]) / normalize_path(path or conflict["path"])
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target.parent)))

    def resolve_conflict(self) -> None:
        conflict = self.selected_conflict()
        if not conflict:
            return
        answer = QMessageBox.question(
            self,
            "Označit konflikt za vyřešený?",
            f"Je soubor {conflict['path']} už ručně sloučený a synchronizovaný?\nOznačení nesmaže žádnou zachovanou kopii.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.submit(
                "action", lambda: self.backend.resolve(conflict["id"]), self.conflict_resolved
            )

    def conflict_resolved(self, _result: dict) -> None:
        self.notify("Konflikt je označený za vyřešený. Zachované soubory zůstávají na místě.")
        self.refresh()

    def save_settings(self) -> None:
        values = {
            "server_url": self.server_url.text().strip(),
            "device_name": self.device_name.text().strip(),
            "allow_insecure": self.insecure.isChecked(),
            "poll_seconds": self.poll_seconds.value(),
            "debounce_ms": self.debounce_ms.value(),
            "max_file_size": self.max_size.value() * 1024 * 1024,
            "ignore": [
                s.strip() for s in self.global_ignore.toPlainText().splitlines() if s.strip()
            ],
        }
        try:
            config = Config(**values)
            config.check_transport()
            if (
                not self.status_data.get("preferences", {}).get("device_id")
                and not self.owner_token.text().strip()
            ):
                self.owner_token.setFocus()
                raise ValueError("Zadejte token vlastníka pro první registraci zařízení.")
        except ValueError as error:
            self.settings_failed(str(error))
            return
        self.settings_error.clear()
        token = self.owner_token.text()
        self.submit(
            "settings",
            lambda: self.backend.configure(values, token),
            self.settings_saved,
            self.settings_failed,
        )

    def settings_failed(self, message: str) -> None:
        self.settings_error.setText(message)
        self.notify(message, error=True)

    def settings_saved(self, prefs: dict) -> None:
        self.owner_token.clear()
        self.show_token.setChecked(False)
        self.populate_preferences(prefs)
        self.notify("Nastavení je uložené. Spusťte synchronizaci tlačítkem vlevo.")
        self.refresh()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.timer.stop()
        if self.jobs:
            self.closing = True
            self.setEnabled(False)
            self.controls()
            event.ignore()
        else:
            event.accept()
