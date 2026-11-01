# -*- coding: utf-8 -*-

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QHeaderView, QLabel, QProgressBar,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)


# 全部队列记录独立显示，关闭窗口只隐藏记录，不停止传输
class TransferHistory(QDialog):
    sigCancel = Signal(int)
    sigClear = Signal(int)
    sigClearCompleted = Signal()

    # 持有的只是界面快照，传输仍由队列控制
    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("传输记录")
        self.resize(920, 500)
        self.setMinimumSize(720, 340)
        self.setModal(False)
        self._tmRow = {}
        lay = QVBoxLayout(self)
        head = QHBoxLayout()
        self.summaryLabel = QLabel("暂无传输记录")
        head.addWidget(self.summaryLabel, 1)
        self.clearDoneBtn = QPushButton("清空已完成")
        self.clearDoneBtn.clicked.connect(self.sigClearCompleted.emit)
        head.addWidget(self.clearDoneBtn)
        lay.addLayout(head)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["名称", "方向", "状态", "进度", "操作"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        for col, width in enumerate((300, 80, 90, 170, 130)):
            self.table.setColumnWidth(col, width)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.table, 1)

    # 打开或清除记录时重建，平时只更新发生变化的行
    def setItems(self, tlTm, tmSummary):
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(0)
            self._tmRow.clear()
            for tm in reversed(tlTm):
                self._insertRow(tm, self.table.rowCount())
            self.setSummary(tmSummary)
        finally:
            self.table.setUpdatesEnabled(True)

    # 记录数、未完成数和失败数始终与气泡及按钮一致
    def setSummary(self, tm):
        self.summaryLabel.setText("共 %d 条  ·  未完成 %d  ·  失败 %d" % (tm["historyCount"], tm["pending"], tm["failedCount"]))
        self.clearDoneBtn.setEnabled(tm["completedCount"] > 0)

    # 进度回调只修改该行，新任务放在最上方
    def updateItem(self, tm):
        if tm["hidden"]:
            return
        row = self.rowOf(tm["id"])
        if row < 0:
            self._insertRow(tm, 0)
        else:
            self._paintRow(row, tm)

    # 查找行时不遍历整个表格
    def rowOf(self, itemId):
        return self._tmRow.get(itemId, -1)

    # 每行保留中断和仅已完成可用的清除
    def _insertRow(self, tm, row):
        for itemId in self._tmRow:
            if self._tmRow[itemId] >= row:
                self._tmRow[itemId] += 1
        self.table.insertRow(row)
        self._tmRow[tm["id"]] = row
        for col, text in enumerate((tm["name"], tm["kind"], tm["state"])):
            item = QTableWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, tm["id"])
            self.table.setItem(row, col, item)
        bar = QProgressBar()
        bar.setRange(0, 100)
        self.table.setCellWidget(row, 3, bar)
        actions = QWidget()
        buttons = QHBoxLayout(actions)
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(4)
        actions.cancelBtn = QPushButton("中断")
        actions.cancelBtn.clicked.connect(lambda _checked=False, itemId=tm["id"]: self.sigCancel.emit(itemId))
        actions.clearBtn = QPushButton("清除")
        actions.clearBtn.clicked.connect(lambda _checked=False, itemId=tm["id"]: self.sigClear.emit(itemId))
        buttons.addWidget(actions.cancelBtn)
        buttons.addWidget(actions.clearBtn)
        self.table.setCellWidget(row, 4, actions)
        self._paintRow(row, tm)

    # 文件名、状态、进度和错误提示保留完整信息
    def _paintRow(self, row, tm):
        self.table.item(row, 2).setText(tm["state"])
        detail = tm["error"] or tm["remotePath"] or tm["localPath"]
        self.table.item(row, 0).setToolTip(tm["name"] + ("\n" + detail if detail else ""))
        self.table.item(row, 2).setToolTip(tm["error"])
        percent = itemPercent(tm)
        bar = self.table.cellWidget(row, 3)
        if tm["state"] == "传输中" and tm["total"] <= 0:
            bar.setRange(0, 0)
            bar.setFormat("准备中…")
        else:
            bar.setRange(0, 100)
            bar.setValue(percent)
            bar.setFormat("%d%%  %s/s" % (percent, formatSpeed(tm["speed"])))
        actions = self.table.cellWidget(row, 4)
        actions.cancelBtn.setEnabled(tm["state"] == "等待" or (tm["state"] == "传输中" and tm["kind"] != "移动"))
        actions.clearBtn.setEnabled(tm["state"] == "完成")


# 无字节总数时不伪造完成百分比
def itemPercent(tm):
    if tm["state"] == "完成":
        return 100
    return 0 if tm["total"] <= 0 else max(0, min(100, int(tm["done"] * 100 / tm["total"])))


# 两个记录视图使用相同的速度显示
def formatSpeed(speed):
    n = float(speed)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (int(n), unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024
    return "0 B"
