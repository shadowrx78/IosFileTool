# -*- coding: utf-8 -*-

from datetime import datetime

from PySide6.QtGui import QFont, QTextCursor

from src.diagnostics import configureLogging
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QPlainTextEdit, QPushButton, QVBoxLayout


# 底部日志。标题上的勾选用来展开，默认收起
class LogPanel(QGroupBox):
    # parent 为主窗口底部
    def __init__(self, parent=None):
        super().__init__("日志", parent)
        self.setCheckable(True)
        self.setChecked(False)
        self._build()
        self.toggled.connect(self._onToggle)
        self._onToggle(False)

    # 搭文本和清空
    def _build(self):
        root = QVBoxLayout(self)
        head = QHBoxLayout()
        self._btnClear = QPushButton("清空")
        self._btnClear.setFixedWidth(52)
        self._btnClear.clicked.connect(self.clear)
        head.addWidget(self._btnClear)
        head.addStretch(1)
        root.addLayout(head)

        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setMaximumBlockCount(4000)
        self._text.setMinimumHeight(120)
        font = QFont("Consolas")
        font.setPointSize(10)
        self._text.setFont(font)
        root.addWidget(self._text, 1)

    # 收起时只留标题
    def _onToggle(self, opened):
        self._btnClear.setVisible(opened)
        self._text.setVisible(opened)
        if opened:
            self.setMaximumHeight(220)
        else:
            self.setMaximumHeight(36)

    # 追加一行带时间
    def info(self, message):
        configureLogging().info(message)
        stamp = datetime.now().strftime("%H:%M:%S")
        self._text.appendPlainText("[%s] %s" % (stamp, message))
        self._text.moveCursor(QTextCursor.MoveOperation.End)

    # 清空
    def clear(self):
        self._text.clear()
