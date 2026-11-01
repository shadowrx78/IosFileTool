# -*- coding: utf-8 -*-

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton,
                               QScrollArea, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from src.transferHistory import formatSpeed, itemPercent


# 常驻工具栏的下载式按钮：环形进度、运行状态和剩余任务数
class TransferButton(QToolButton):
    # 图标按当前状态绘制，无需外部图片资源
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("transferButton")
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setIconSize(QSize(28, 28))
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(34)
        self._tmSummary = {"state": "idle", "pending": 0, "percent": None, "historyCount": 0, "failedCount": 0}
        self._angle = 0
        self._spinner = QTimer(self)
        self._spinner.setInterval(90)
        self._spinner.timeout.connect(self._animate)
        self.setSummary(self._tmSummary)

    # 只有准备中的未知进度使用旋转环，正常传输显示真实批次百分比
    def setSummary(self, tm):
        self._tmSummary = dict(tm)
        state = tm["state"]
        if state == "running":
            progress = "准备中" if tm["percent"] is None else "%d%%" % tm["percent"]
            self.setText("%s · %d 项" % (progress, tm["pending"]))
            tooltip = "传输中，队列剩余 %d 项；整体进度按本批任务数统计。点击查看最近传输。" % tm["pending"]
        elif state == "failed":
            self.setText("传输异常")
            tooltip = "传输已停止，存在失败记录。点击查看原因。"
        elif state == "done":
            self.setText("传输完成")
            tooltip = "本批传输已完成。点击查看最近记录。"
        elif state == "cancelled":
            self.setText("已中断")
            tooltip = "本批传输已结束，存在中断记录。"
        else:
            self.setText("传输")
            tooltip = "暂无正在进行的传输。点击查看最近记录。"
        self.setToolTip(tooltip)
        self.setAccessibleName("传输队列：" + self.text())
        if state == "running" and tm["percent"] is None:
            if not self._spinner.isActive():
                self._spinner.start()
        else:
            self._spinner.stop()
        self._drawIcon()

    # 准备阶段只旋转图标，不重建记录或刷新整张历史表
    def _animate(self):
        self._angle = (self._angle + 30) % 360
        self._drawIcon()

    # 环形进度与中心箭头、完成勾或错误标记共同表明状态
    def _drawIcon(self):
        tm = self._tmSummary
        color = {"running": "#66b5ff", "done": "#6bd3a4", "failed": "#ff9b87"}.get(tm["state"], "#a6b0bd")
        pixmap = QPixmap(56, 56)
        pixmap.setDevicePixelRatio(2)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#444b55"), 2))
        ring = QRectF(2, 2, 24, 24)
        painter.drawEllipse(ring)
        painter.setPen(QPen(QColor(color), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if tm["state"] == "running":
            if tm["percent"] is None:
                painter.drawArc(ring, (90-self._angle)*16, -100*16)
            else:
                painter.drawArc(ring, 90*16, -max(1, tm["percent"])*360*16//100)
        elif tm["state"] == "done":
            painter.drawEllipse(ring)
        if tm["state"] == "done":
            painter.drawLine(9, 14, 13, 18)
            painter.drawLine(13, 18, 20, 10)
        elif tm["state"] == "failed":
            painter.drawLine(14, 8, 14, 15)
            painter.drawPoint(14, 19)
        else:
            painter.drawLine(14, 7, 14, 18)
            painter.drawLine(9, 13, 14, 18)
            painter.drawLine(14, 18, 19, 13)
            painter.drawLine(9, 22, 19, 22)
        painter.end()
        self.setIcon(QIcon(pixmap))


# 点击按钮才出现的轻量气泡，最多保留六张可见记录卡
class TransferPopup(QFrame):
    sigCancel = Signal(int)
    sigClear = Signal(int)
    sigShowAll = Signal()
    sigVisibility = Signal(bool)

    # Qt.Popup 自动处理外部点击与 Escape 收起，不影响后台队列
    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("transferPopup")
        self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay, True)
        self.setFixedWidth(430)
        self._tmCard = {}
        self._tlId = []
        self._header = QVBoxLayout(self)
        self._header.setContentsMargins(14, 12, 14, 12)
        title = QLabel("最近传输")
        title.setStyleSheet("font-weight: bold; font-size: 14px; background: transparent;")
        self._header.addWidget(title)
        self.summaryLabel = QLabel("暂无传输记录")
        self.summaryLabel.setObjectName("transferMuted")
        self._header.addWidget(self.summaryLabel)
        self.scroll = QScrollArea()
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setObjectName("transferScroll")
        self._cards = QWidget()
        self._cards.setObjectName("transferCards")
        self._cardLayout = QVBoxLayout(self._cards)
        self._cardLayout.setContentsMargins(0, 0, 0, 0)
        self._cardLayout.setSpacing(5)
        self.scroll.setWidget(self._cards)
        self._header.addWidget(self.scroll)
        self.emptyLabel = QLabel("暂无传输记录")
        self.emptyLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.emptyLabel.setMinimumHeight(70)
        self._header.addWidget(self.emptyLabel)
        self.showAllBtn = QPushButton("显示全部传输记录")
        self.showAllBtn.clicked.connect(self.sigShowAll.emit)
        self._header.addWidget(self.showAllBtn)

    # 当前传输优先，随后是最近记录；更新进度不会反复销毁鼠标下的按钮
    def setItems(self, tlTm, tmSummary):
        tlId = [tm["id"] for tm in tlTm]
        if tlId != self._tlId:
            while self._cardLayout.count():
                widget = self._cardLayout.takeAt(0).widget()
                if widget is not None:
                    widget.deleteLater()
            self._tmCard.clear()
            for tm in tlTm:
                card = _TransferCard(tm)
                card.sigCancel.connect(self.sigCancel)
                card.sigClear.connect(self.sigClear)
                self._tmCard[tm["id"]] = card
                self._cardLayout.addWidget(card)
            self._tlId = tlId
        for tm in tlTm:
            self._tmCard[tm["id"]].updateItem(tm)
        self.summaryLabel.setText("%d 项未完成  ·  最近 %d 条" % (tmSummary["pending"], len(tlTm)))
        self.emptyLabel.setVisible(not tlTm)
        self.scroll.setVisible(bool(tlTm))
        availableHeight = self.screen().availableGeometry().height()
        self.scroll.setFixedHeight(min(len(tlTm)*72, max(90, availableHeight-170)) if tlTm else 0)
        self.adjustSize()

    # 气泡对齐按钮右侧，并限制在当前屏幕的可用区域内
    def openAt(self, button):
        available = button.screen().availableGeometry()
        self.adjustSize()
        anchor = button.mapToGlobal(QPoint(button.width(), button.height()+8))
        x = max(available.left()+8, min(anchor.x()-self.width(), available.right()-self.width()-8))
        y = anchor.y()
        if y+self.height() > available.bottom()-8:
            y = max(available.top()+8, button.mapToGlobal(QPoint(0, 0)).y()-self.height()-8)
        self.move(x, y)
        self.show()

    # Escape 明确收起气泡，不把按键传给主窗口的文件操作
    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            event.accept()
            return
        super().keyPressEvent(event)

    # 主按钮的按下状态与气泡显示一致
    def showEvent(self, event):
        super().showEvent(event)
        self.sigVisibility.emit(True)

    # 外部点击或 Escape 收起也要同步主按钮
    def hideEvent(self, event):
        super().hideEvent(event)
        self.sigVisibility.emit(False)


# 单条最近记录，长文件名省略显示，完整名称和错误保留在提示中
class _TransferCard(QFrame):
    sigCancel = Signal(int)
    sigClear = Signal(int)

    # 每张卡只包含简短标题、状态、细进度条和一个当前可用动作
    def __init__(self, tm):
        super().__init__()
        self.setObjectName("transferCard")
        self.itemId = tm["id"]
        lay = QVBoxLayout(self)
        lay.setContentsMargins(9, 7, 9, 7)
        lay.setSpacing(4)
        row = QHBoxLayout()
        self.nameLabel = QLabel()
        self.nameLabel.setTextFormat(Qt.TextFormat.PlainText)
        self.nameLabel.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(self.nameLabel, 1)
        self.actionBtn = QToolButton()
        self.actionBtn.setObjectName("transferRowAction")
        self.actionBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.actionBtn.clicked.connect(self._act)
        row.addWidget(self.actionBtn)
        lay.addLayout(row)
        self.detailLabel = QLabel()
        self.detailLabel.setObjectName("transferMuted")
        lay.addWidget(self.detailLabel)
        self.bar = QProgressBar()
        self.bar.setObjectName("transferThinProgress")
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(4)
        self._barColor = ""
        lay.addWidget(self.bar)
        self.setFixedHeight(67)
        self._state = tm["state"]
        self.updateItem(tm)

    # 只清已完成项，失败项保留错误提示，进行中可直接中断
    def updateItem(self, tm):
        self._state = tm["state"]
        self.nameLabel.setText(self.fontMetrics().elidedText(tm["name"], Qt.TextElideMode.ElideMiddle, 300))
        self.nameLabel.setToolTip(tm["name"])
        percent = itemPercent(tm)
        detail = "%s · %s" % (tm["kind"], tm["state"])
        if tm["state"] == "传输中" and tm["total"] > 0:
            detail += " · %d%% · %s/s" % (percent, formatSpeed(tm["speed"]))
        self.detailLabel.setText(detail)
        self.setToolTip(tm["error"] or tm["remotePath"] or tm["localPath"])
        unknown = tm["state"] == "传输中" and tm["total"] <= 0
        self.bar.setRange(0, 0 if unknown else 100)
        if not unknown:
            self.bar.setValue(percent)
        color = "#ed8c7b" if tm["state"] == "失败" else "#62c99a" if tm["state"] == "完成" else "#66b5ff"
        if color != self._barColor:
            self.bar.setStyleSheet("QProgressBar::chunk { background: %s; }" % color)
            self._barColor = color
        self.actionBtn.setText("清除" if tm["state"] == "完成" else "中断")
        enabled = tm["state"] == "完成" or tm["state"] == "等待" or (tm["state"] == "传输中" and tm["kind"] != "移动")
        self.actionBtn.setVisible(tm["state"] in ("完成", "等待", "传输中", "正在中断"))
        self.actionBtn.setEnabled(enabled)

    # 操作发给队列，不由界面直接修改传输状态
    def _act(self):
        if self._state == "完成":
            self.sigClear.emit(self.itemId)
        else:
            self.sigCancel.emit(self.itemId)
