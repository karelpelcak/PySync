from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from pysync_client_gui.backend import Backend
from pysync_client_gui.widgets import button, label


class AddProjectDialog(QDialog):
    def __init__(self, backend: Backend, submit: Callable, parent: QWidget):
        super().__init__(parent)
        self.backend, self.submit = backend, submit
        self.plan_data: dict | None = None
        self.plan_arguments: dict | None = None
        self.projects_loaded = False
        self.applying = False
        self.setWindowTitle("Připojit projekt · PySync")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.resize(610, 700)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(15)
        layout.addWidget(label("Připojit projekt", "title"))
        layout.addWidget(
            label("Vyberte složku a zkontrolujte změny před první synchronizací.", "subtitle", True)
        )
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setSpacing(10)
        self.root = QLineEdit()
        self.root.setPlaceholderText("Vyberte existující složku…")
        self.root.setMaxLength(4096)
        self.browse_button = button("Vybrat složku…")
        self.browse_button.clicked.connect(self.browse)
        row = QHBoxLayout()
        row.addWidget(self.root, 1)
        row.addWidget(self.browse_button)
        form.addRow("&Složka na tomto zařízení", row)
        self.name = QLineEdit()
        self.name.setMaxLength(100)
        form.addRow("&Název projektu na tomto zařízení", self.name)
        self.server = QComboBox()
        self.server.addItem("Načítám projekty serveru…", None)
        self.server.setEnabled(False)
        form.addRow("&Projekt na serveru", self.server)
        self.ignore = QPlainTextEdit()
        self.ignore.setPlaceholderText(
            "build/\n*.log\nJeden vzor na řádek; globální pravidla platí také."
        )
        self.ignore.setMaximumHeight(85)
        form.addRow("Další &ignorované soubory", self.ignore)
        layout.addLayout(form)
        self.accept_local = QCheckBox("Nahrát také existující lokální soubory")
        self.accept_local.setToolTip(
            "Rozdílné lokální soubory se uloží jako konfliktní kopie na serveru."
        )
        layout.addWidget(self.accept_local)
        layout.addWidget(
            label(
                "Bez této volby zůstanou lokální soubory navíc pouze zde. Rozdílný lokální obsah se před stažením uchová v .pysync-recovery.",
                "subtitle",
                True,
            )
        )
        self.review = QPlainTextEdit()
        self.review.setReadOnly(True)
        self.review.setPlaceholderText("Nejprve zkontrolujte plán synchronizace.")
        self.review.setMinimumHeight(115)
        self.review.setAccessibleName("Plán počáteční synchronizace")
        layout.addWidget(self.review, 1)
        self.error = label("", wrap=True)
        self.error.setStyleSheet("color: #ffaeb5;")
        layout.addWidget(self.error)
        actions = QHBoxLayout()
        self.cancel = button("Zrušit")
        self.cancel.clicked.connect(self.reject)
        self.preview = button("Zkontrolovat změny")
        self.preview.setEnabled(False)
        self.preview.clicked.connect(self.preview_plan)
        self.apply = button("Připojit a synchronizovat", True)
        self.apply.setEnabled(False)
        self.apply.clicked.connect(self.apply_plan)
        actions.addWidget(self.cancel)
        actions.addStretch()
        actions.addWidget(self.preview)
        actions.addWidget(self.apply)
        layout.addLayout(actions)
        for field in (self.root, self.name):
            field.textChanged.connect(self.invalidate)
        self.server.currentIndexChanged.connect(self.invalidate)
        self.ignore.textChanged.connect(self.invalidate)
        self.accept_local.toggled.connect(self.invalidate)

    def load_projects(self) -> None:
        self.submit("dialog-projects", self.backend.projects, self.loaded, self.projects_failed)

    def projects_failed(self, message: str) -> None:
        self.error.setText(message)
        self.preview.setText("Zkusit načíst projekty")
        self.preview.setEnabled(True)

    def loaded(self, result: dict) -> None:
        self.projects_loaded = True
        self.error.clear()
        self.server.clear()
        self.server.addItem("Vytvořit nový / použít shodný název", None)
        for project in result["projects"]:
            self.server.addItem(project["name"], project["id"])
        self.server.setEnabled(True)
        self.preview.setText("Zkontrolovat změny")
        self.preview.setEnabled(True)

    def failed(self, message: str) -> None:
        self.error.setText(message)
        self.applying = False
        self.cancel.setEnabled(True)
        self.apply.setText("Připojit a synchronizovat")
        self.preview.setEnabled(self.projects_loaded)
        self.apply.setEnabled(False)
        self.set_fields_enabled(True)

    def browse(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Vyberte složku projektu")
        if selected:
            self.root.setText(selected)
            if not self.name.text().strip():
                self.name.setText(Path(selected).name)

    def invalidate(self, *_args: Any) -> None:
        if self.plan_data is not None:
            self.review.setPlainText("Výběr se změnil. Zkontrolujte nový plán synchronizace.")
        self.plan_data = None
        self.plan_arguments = None
        self.apply.setEnabled(False)

    def arguments(self) -> dict:
        root = self.root.text().strip()
        if not root or not Path(root).is_dir():
            self.root.setFocus()
            raise ValueError("Vyberte existující složku projektu.")
        name = self.name.text().strip() or Path(root).name
        patterns = [line.strip() for line in self.ignore.toPlainText().splitlines() if line.strip()]
        return {
            "root": str(Path(root).absolute()),
            "name": name,
            "server_project": self.server.currentData(),
            "ignore": patterns,
            "accept_local": self.accept_local.isChecked(),
        }

    def set_fields_enabled(self, enabled: bool) -> None:
        for widget in (self.root, self.name, self.browse_button, self.ignore, self.accept_local):
            widget.setEnabled(enabled)
        self.server.setEnabled(enabled and self.projects_loaded)

    def preview_plan(self) -> None:
        if not self.projects_loaded:
            self.preview.setEnabled(False)
            self.load_projects()
            return
        try:
            arguments = self.arguments()
        except ValueError as error:
            self.failed(str(error))
            return
        self.error.clear()
        self.review.setPlainText("Kontroluji lokální soubory a obsah serveru…")
        self.preview.setEnabled(False)
        self.apply.setEnabled(False)
        self.set_fields_enabled(False)
        self.plan_arguments = arguments
        self.submit(
            "dialog-plan", lambda: self.backend.plan(**arguments), self.show_plan, self.failed
        )

    def show_plan(self, result: dict) -> None:
        if self.plan_arguments is None:
            return
        self.plan_data = result
        self.plan_arguments["server_project"] = result["project_id"]
        self.review.setPlainText(
            f"Lokální soubory: {result['local_files']}\n"
            f"Soubory ke stažení: {result['downloads']}\n"
            f"Lokální soubory navíc: {result['local_only']}\n"
            f"Rozdílné soubory: {len(result['collisions'])}\n\n"
            + (
                "Lokální soubory nahrajeme; rozdíly zachováme jako konfliktní kopie."
                if result["accept_local"]
                else "Lokální soubory navíc nenahrajeme; rozdíly zachováme v .pysync-recovery."
            )
            + (
                "\n\nRozdíly:\n" + "\n".join(result["collisions"][:30])
                if result["collisions"]
                else ""
            )
        )
        self.set_fields_enabled(True)
        self.preview.setEnabled(True)
        self.apply.setEnabled(True)

    def apply_plan(self) -> None:
        if not self.plan_data or not self.plan_arguments:
            return
        self.apply.setEnabled(False)
        self.preview.setEnabled(False)
        self.set_fields_enabled(False)
        self.applying = True
        self.cancel.setEnabled(False)
        self.apply.setText("Připojuji projekt…")
        arguments = self.plan_arguments.copy()
        self.submit(
            "dialog-add", lambda: self.backend.add(**arguments), self.accepted_plan, self.failed
        )

    def accepted_plan(self, _result: dict) -> None:
        self.applying = False
        self.accept()

    def reject(self) -> None:
        if not self.applying:
            super().reject()
