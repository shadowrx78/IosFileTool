# -*- coding: utf-8 -*-

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QStyleFactory


_DARK_QSS = """
QWidget { color: #e8e8e8; background-color: #2b2b2b; }
QMainWindow, QDialog, QMessageBox { background-color: #2b2b2b; }
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QTableWidget {
    background-color: #1e1e1e;
    color: #e8e8e8;
    border: 1px solid #555555;
    selection-background-color: #1565c0;
    selection-color: #ffffff;
}
QHeaderView::section {
    background-color: #333333;
    color: #e8e8e8;
    border: 1px solid #555555;
    padding: 4px;
}
QPushButton {
    background-color: #3c3c3c;
    color: #e8e8e8;
    border: 1px solid #666666;
    padding: 4px 10px;
    min-height: 22px;
}
QPushButton:hover { background-color: #4a4a4a; }
QPushButton:disabled { color: #888888; background-color: #333333; }
QCheckBox { background-color: transparent; spacing: 8px; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid #c0c0c0;
    background-color: #1e1e1e;
}
QCheckBox::indicator:checked { background-color: #1565c0; }
QGroupBox {
    border: 1px solid #555555;
    margin-top: 10px;
    padding-top: 8px;
}
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
QGroupBox::indicator {
    width: 14px; height: 14px;
    border: 1px solid #c0c0c0;
    background-color: #1e1e1e;
}
QGroupBox::indicator:checked { background-color: #1565c0; }
QGroupBox {
    border: 1px solid #555555;
    margin-top: 10px;
    padding-top: 8px;
}
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
QGroupBox::indicator {
    width: 14px; height: 14px;
    border: 1px solid #c0c0c0;
    background-color: #1e1e1e;
}
QGroupBox::indicator:checked { background-color: #1565c0; }
QMenu { background-color: #252525; border: 1px solid #666666; padding: 4px; }
QMenu::item { padding: 6px 24px; background-color: transparent; }
QMenu::item:selected:enabled { background-color: #1565c0; color: #ffffff; }
QMenu::item:disabled { color: #888888; }
QMenu::separator { height: 1px; background-color: #555555; margin: 4px 8px; }
QTableWidget { gridline-color: #444444; }
QToolButton#transferButton {
    background: #333a43; border: 1px solid #555e6c; border-radius: 7px;
    padding: 2px 10px; color: #e3eaf3;
}
QToolButton#transferButton:hover, QToolButton#transferButton:checked { background: #414d5d; border-color: #7795ba; }
QFrame#transferPopup { background: #292e36; border: 1px solid #626e7d; border-radius: 9px; }
QFrame#transferPopup QLabel, QWidget#transferCards, QScrollArea#transferScroll,
QScrollArea#transferScroll > QWidget { background: transparent; }
QFrame#transferCard { background: #353d48; border: 1px solid #495362; border-radius: 6px; }
QLabel#transferMuted { color: #b4bfcd; background: transparent; font-size: 11px; }
QToolButton#transferRowAction { background: transparent; border: 0; color: #9acbff; padding: 2px 6px; }
QToolButton#transferRowAction:hover { background: #4a5c72; border-radius: 4px; }
QToolButton#transferRowAction:disabled { color: #858e9b; }
QProgressBar#transferThinProgress { border: 0; border-radius: 2px; background: #232831; }
QProgressBar#transferThinProgress::chunk { border-radius: 2px; background: #66b5ff; }
"""


# 给 QApplication 套深色 Fusion
def applyDarkTheme(app):
    if "Fusion" in QStyleFactory.keys():
        app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(43, 43, 43))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(232, 232, 232))
    palette.setColor(QPalette.ColorRole.Base, QColor(30, 30, 30))
    palette.setColor(QPalette.ColorRole.Text, QColor(232, 232, 232))
    palette.setColor(QPalette.ColorRole.Button, QColor(60, 60, 60))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(232, 232, 232))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(25, 118, 210))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    app.setPalette(palette)
    app.setStyleSheet(_DARK_QSS)
