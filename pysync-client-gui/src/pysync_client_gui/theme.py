import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

STYLE = """
QWidget { color: #e7eefb; font-size: 13px; }
QMainWindow, QWidget#workspace { background: #0b1120; }
QFrame#sidebar { background: #10192b; border-right: 1px solid #23314b; }
QLabel { background: transparent; }
QLabel[role="brand"] { font-size: 27px; font-weight: 750; }
QLabel[role="eyebrow"] { color: #8fa6c7; font-size: 10px; font-weight: 650; }
QLabel[role="title"] { font-size: 27px; font-weight: 700; }
QLabel[role="subtitle"] { color: #a7b8d3; font-size: 13px; }
QLabel[role="section"] { font-size: 17px; font-weight: 650; }
QLabel[role="number"] { font-size: 31px; font-weight: 700; }
QLabel[role="pill"] { background: #1d2c46; color: #b9cbea; border-radius: 13px; padding: 6px 12px; }
QLabel[role="pill"][online="true"] { background: #12382e; color: #8aefd0; }
QFrame[role="card"] { background: #121e33; border: 1px solid #263651; border-radius: 12px; }
QFrame#notice { background: #17352e; border: 1px solid #315a48; border-radius: 8px; }
QFrame#notice[error="true"] { background: #3c242b; border-color: #81505a; }
QPushButton { background: #1b2c46; border: 1px solid #324562; border-radius: 7px; padding: 9px 14px; font-weight: 550; }
QPushButton:hover { background: #263c5a; }
QPushButton:pressed { background: #304c6d; }
QPushButton:disabled { color: #7890b1; background: #16243a; border-color: #25364e; }
QPushButton:focus { border: 2px solid #76e6ca; }
QPushButton[primary="true"] { color: #082e29; background: #76e6ca; border-color: #76e6ca; }
QPushButton[primary="true"]:hover { background: #9cf0dc; }
QPushButton[primary="true"]:disabled { background: #274d48; color: #85aaa3; border-color: #355d57; }
QPushButton[nav="true"] { background: transparent; border: none; text-align: left; padding: 12px 16px; color: #a7b8d3; }
QPushButton[nav="true"]:checked { background: #233c47; color: #91efda; }
QPushButton[nav="true"]:focus { border: 1px solid #76e6ca; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit { background: #0e182b; border: 1px solid #344965; border-radius: 6px; padding: 8px; selection-background-color: #315a69; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus { border: 2px solid #76e6ca; }
QLineEdit:disabled { color: #94a4bd; }
QComboBox QAbstractItemView { background: #17253a; selection-background-color: #315a69; }
QCheckBox { spacing: 9px; padding: 5px 0; }
QCheckBox::indicator { width: 18px; height: 18px; border: 1px solid #6882a1; border-radius: 4px; background: #0e182b; }
QCheckBox::indicator:checked { background: #76e6ca; border-color: #76e6ca; image: url(:/qt-project.org/styles/commonstyle/images/standardbutton-apply-16.png); }
QCheckBox:focus { outline: 1px solid #76e6ca; }
QTableWidget { background: #121e33; alternate-background-color: #15233a; border: 1px solid #263651; border-radius: 8px; gridline-color: #263651; selection-background-color: #254652; selection-color: #ebfff8; }
QTableWidget::item { padding: 10px 8px; }
QHeaderView::section { background: #19283f; color: #a7b8d3; border: none; padding: 10px 8px; font-size: 11px; font-weight: 650; }
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QProgressBar { border: none; background: #22354c; border-radius: 4px; height: 8px; text-align: center; }
QProgressBar::chunk { background: #76e6ca; border-radius: 4px; }
QDialog, QMessageBox { background: #101a2c; }
QToolTip { color: #e7eefb; background: #243851; border: 1px solid #49627e; padding: 5px; }
"""


def icon() -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#76e6ca"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(2, 2, 60, 60, 16, 16)
    painter.setPen(QColor("#0d3430"))
    painter.setFont(QFont("Arial", 35, QFont.Weight.Bold))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "⇄")
    painter.end()
    return QIcon(pixmap)


def apply_theme(application: QApplication) -> None:
    application.setStyle("Fusion")
    application.setFont(QFont("Helvetica Neue" if sys.platform == "darwin" else "Sans Serif", 11))
    application.setStyleSheet(STYLE)
    application.setWindowIcon(icon())
