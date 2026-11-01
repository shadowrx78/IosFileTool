# -*- coding: utf-8 -*-

import json
import logging

from PySide6.QtCore import QItemSelection, QItemSelectionModel, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QKeySequence, QShortcut, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QRubberBand,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


# 条目上直接进入拖动；空白处仍由 Qt 框选，Ctrl / Shift 保留原生多选
class _DragGate:
    # 无修饰键按在条目上时自行保存手势，避免第一次拖动进入 DragSelectingState
    def mousePressEvent(self, event):
        self._phonePress = None
        self._phoneBlankPress = None
        if event.button() == Qt.MouseButton.LeftButton and not event.modifiers():
            pos = event.position().toPoint()
            idx = self.indexAt(pos)
            if idx.isValid():
                self._phonePress = pos
                self._phoneWasSelected = self.selectionModel().isSelected(idx)
                if not self._phoneWasSelected:
                    flags = QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows
                    self.selectionModel().select(idx, flags)
                self.selectionModel().setCurrentIndex(idx, QItemSelectionModel.SelectionFlag.NoUpdate)
                self.setFocus()
                event.accept()
                return
            self._phoneBlankPress = pos
            self.clearSelection()
            event.accept()
            return
        super().mousePressEvent(event)

    # 超过系统拖动阈值立即交给 OLE，不把条目上的移动传给框选处理
    def mouseMoveEvent(self, event):
        blank = getattr(self, "_phoneBlankPress", None)
        if blank is not None and event.buttons() & Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            if (pos - blank).manhattanLength() >= QApplication.startDragDistance():
                rect = QRect(blank, pos).normalized()
                if not hasattr(self, "_selectionBand"):
                    self._selectionBand = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())
                self._selectionBand.setGeometry(rect)
                self._selectionBand.show()
                self._selectInBand(rect)
            event.accept()
            return
        press = getattr(self, "_phonePress", None)
        if press is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if (event.position().toPoint() - press).manhattanLength() >= QApplication.startDragDistance():
                self._phonePress = None
                self.startDrag(Qt.DropAction.CopyAction)
            event.accept()
            return
        super().mouseMoveEvent(event)

    # 以实际条目矩形框选；表格按整行判断，避免起点在空白时 Qt 不选择
    def _selectInBand(self, rect):
        selection = QItemSelection()
        for row in range(self.model().rowCount()):
            idx = self.model().index(row, 0)
            itemRect = self.visualRect(idx)
            if isinstance(self, QTableWidget):
                itemRect.setLeft(0)
                itemRect.setRight(self.viewport().width())
            if rect.intersects(itemRect):
                selection.select(idx, idx)
        flags = QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows
        self.selectionModel().select(selection, flags)

    # 普通点击原已选中的项时收拢选择；拖动时保留整组选择
    def mouseReleaseEvent(self, event):
        blank = getattr(self, "_phoneBlankPress", None)
        self._phoneBlankPress = None
        if hasattr(self, "_selectionBand"):
            self._selectionBand.hide()
        if blank is not None and event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            return
        press = getattr(self, "_phonePress", None)
        self._phonePress = None
        if press is not None and event.button() == Qt.MouseButton.LeftButton:
            idx = self.indexAt(press)
            if idx.isValid() and self._phoneWasSelected:
                flags = QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows
                self.selectionModel().select(idx, flags)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # 第一击被自定义选择处理接管，第二击显式发出原生双击信号
    def mouseDoubleClickEvent(self, event):
        idx = self.indexAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and idx.isValid():
            self._phonePress = None
            self.doubleClicked.emit(idx)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    # 当前选择已经固定，直接请求主窗口启动一次虚拟拖放
    def startDrag(self, _actions):
        self.sigDragOut.emit()


# 既能接收从资源管理器拖进来的文件，也能开始向外拖
class _DropTable(_DragGate, QTableWidget):
    sigDropIn = Signal(list, str)
    sigMove = Signal(list, str)
    sigDragOut = Signal()

    # 允许放下本地文件
    def __init__(self):
        super().__init__(0, 3)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setDragDropOverwriteMode(False)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)

    # 电脑文件，或手机里拖到别的文件夹
    def dragEnterEvent(self, event):
        if _canDropHere(event.mimeData()):
            _acceptDrop(event)
        else:
            event.ignore()

    # 拖着经过时保持接收
    def dragMoveEvent(self, event):
        if _canDropAt(self, event):
            _acceptDrop(event)
        else:
            event.ignore()

    # 落在文件夹上才有目标目录。落在文件或空白上，手机内容不复制
    def dropEvent(self, event):
        if _takeDrop(self, event):
            _acceptDrop(event)
        else:
            event.ignore()


# 项目视图同样支持拖入拖出
class _DropList(_DragGate, QListWidget):
    sigDropIn = Signal(list, str)
    sigMove = Signal(list, str)
    sigDragOut = Signal()

    # 图标格子
    def __init__(self):
        super().__init__()
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setIconSize(QSize(64, 64))
        self.setGridSize(QSize(108, 96))
        self.setSpacing(8)
        self.setWordWrap(True)
        self.setUniformItemSizes(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setDragDropOverwriteMode(False)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)

    # 电脑文件，或手机里拖到别的文件夹
    def dragEnterEvent(self, event):
        if _canDropHere(event.mimeData()):
            _acceptDrop(event)
        else:
            event.ignore()

    # 拖着经过时保持接收
    def dragMoveEvent(self, event):
        if _canDropAt(self, event):
            _acceptDrop(event)
        else:
            event.ignore()

    # 和列表同一套放下规则
    def dropEvent(self, event):
        if _takeDrop(self, event):
            _acceptDrop(event)
        else:
            event.ignore()


# 手机当前目录：搜索、列表/项目、右键
class FileBrowser(QWidget):
    sigActivated = Signal(object)
    sigDropIn = Signal(list, str)
    sigMove = Signal(list, str)
    sigDragOut = Signal(list)
    sigMenu = Signal(object, object)
    sigDelete = Signal()
    sigCopy = Signal()
    sigCut = Signal()
    sigPaste = Signal()

    # 两套视图共用同一份目录数据
    def __init__(self):
        super().__init__()
        self._tlTmAll = []
        self._gridMode = False
        self._build()

    # 搜索框和视图切换
    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("过滤当前目录里的文件")
        self.search.textChanged.connect(self._refill)
        self.listBtn = QPushButton("列表")
        self.listBtn.clicked.connect(lambda: self.setGridMode(False))
        self.gridBtn = QPushButton("项目")
        self.gridBtn.clicked.connect(lambda: self.setGridMode(True))
        row.addWidget(self.search, 1)
        row.addWidget(self.listBtn)
        row.addWidget(self.gridBtn)
        lay.addLayout(row)

        self.table = _DropTable()
        self.table.setHorizontalHeaderLabels(["名称", "类型", "大小"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setIconSize(QSize(20, 20))
        self.table.cellDoubleClicked.connect(self._onTableActivate)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(lambda pos: self._popup(self.table, pos))
        self.table.sigDropIn.connect(self.sigDropIn)
        self.table.sigMove.connect(self.sigMove)
        self.table.sigDragOut.connect(self._emitDragOut)
        lay.addWidget(self.table, 1)

        self.grid = _DropList()
        self.grid.itemDoubleClicked.connect(self._onGridActivate)
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(lambda pos: self._popup(self.grid, pos))
        self.grid.sigDropIn.connect(self.sigDropIn)
        self.grid.sigMove.connect(self.sigMove)
        self.grid.sigDragOut.connect(self._emitDragOut)
        self.grid.hide()
        lay.addWidget(self.grid, 1)
        self.setGridMode(False)
        self._tlShortcut = []
        for view in (self.table, self.grid):
            for key, callback in (("Delete", self._deleteSelection), ("Ctrl+C", self._copySelection),
                                  ("Ctrl+X", self._cutSelection), ("Ctrl+V", self.sigPaste.emit)):
                shortcut = QShortcut(QKeySequence(key), view)
                shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
                shortcut.setAutoRepeat(False)
                shortcut.activated.connect(callback)
                self._tlShortcut.append(shortcut)

    # 无选中项时 Delete 完全无效，由主窗口复用同一删除确认
    def _deleteSelection(self):
        if self.selectedEntries():
            self.sigDelete.emit()

    # 只复制文件选择，搜索输入框里的 Ctrl+C 保持文本复制
    def _copySelection(self):
        if self.selectedEntries():
            self.sigCopy.emit()

    # 剪切与复制使用同一选择范围
    def _cutSelection(self):
        if self.selectedEntries():
            self.sigCut.emit()

    # 换成这一层目录的内容
    def setEntries(self, tlTmEntry):
        self._tlTmAll = tlTmEntry
        self._refill()

    # 某一条事后确认打不开，立刻改字，不用再进一次
    def markBlocked(self, remotePath):
        hit = False
        for tm in self._tlTmAll:
            if tm.get("remotePath") == remotePath:
                tm["blocked"] = True
                hit = True
        if hit:
            self._refill()

    # 清空
    def clear(self):
        self._tlTmAll = []
        self.table.setRowCount(0)
        self.grid.clear()

    # 列表或项目
    def setGridMode(self, gridMode):
        tlsSelected = {tm["remotePath"] for tm in self.selectedEntries()}
        self._gridMode = gridMode
        self.table.setVisible(not gridMode)
        self.grid.setVisible(gridMode)
        self.listBtn.setEnabled(gridMode)
        self.gridBtn.setEnabled(not gridMode)
        self._refill()
        self._restoreSelection(tlsSelected)

    # 当前视图里选中的项
    def selectedEntries(self):
        tlTm = []
        if self._gridMode:
            for item in self.grid.selectedItems():
                tm = item.data(Qt.ItemDataRole.UserRole)
                if tm is not None:
                    tlTm.append(tm)
            return tlTm
        tlsRow = set()
        for idx in self.table.selectionModel().selectedRows():
            tlsRow.add(idx.row())
        for row in sorted(tlsRow):
            item = self.table.item(row, 0)
            if item is None:
                continue
            tm = item.data(Qt.ItemDataRole.UserRole)
            if tm is not None:
                tlTm.append(tm)
        return tlTm

    # 按搜索框重画当前视图
    def _refill(self):
        tlsSelected = {tm["remotePath"] for tm in self.selectedEntries()}
        key = self.search.text().strip().lower()
        tlTm = []
        for tm in self._tlTmAll:
            if key and key not in tm["name"].lower():
                continue
            tlTm.append(tm)
        if self._gridMode:
            self._fillGrid(tlTm)
        else:
            self._fillTable(tlTm)
        self._restoreSelection(tlsSelected)

    # 搜索、刷新和切换显示方式后按路径恢复选择，避免行号指向另一文件
    def _restoreSelection(self, tlsPath):
        view = self.grid if self._gridMode else self.table
        view.clearSelection()
        flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        for row in range(view.model().rowCount()):
            idx = view.model().index(row, 0)
            tm = idx.data(Qt.ItemDataRole.UserRole)
            if tm and tm["remotePath"] in tlsPath:
                view.selectionModel().select(idx, flags)

    # 列表：名称前带文件夹或文件图标
    def _fillTable(self, tlTm):
        self.table.setRowCount(len(tlTm))
        for row, tm in enumerate(tlTm):
            nameItem = QTableWidgetItem(self._kindIcon(tm["isDir"], bool(tm.get("blocked"))), tm["name"])
            nameItem.setData(Qt.ItemDataRole.UserRole, tm)
            if tm.get("blocked"):
                nameItem.setToolTip("这个目录没有读取权限")
            self.table.setItem(row, 0, nameItem)
            kind = "文件夹" if tm["isDir"] else "文件"
            kindItem = QTableWidgetItem(kind)
            kindItem.setData(Qt.ItemDataRole.UserRole, tm)
            self.table.setItem(row, 1, kindItem)
            sizeText = "" if tm["isDir"] else _formatSize(tm["size"])
            sizeItem = QTableWidgetItem(sizeText)
            sizeItem.setData(Qt.ItemDataRole.UserRole, tm)
            self.table.setItem(row, 2, sizeItem)
        self.table.resizeColumnsToContents()

    # 项目：一行多列，图标区分文件和文件夹
    def _fillGrid(self, tlTm):
        self.grid.clear()
        for tm in tlTm:
            label = tm["name"]
            if not tm["isDir"]:
                label = "%s\n%s" % (tm["name"], _formatSize(tm["size"]))
            item = QListWidgetItem(self._kindIcon(tm["isDir"], bool(tm.get("blocked")), 64), label)
            if tm.get("blocked"):
                item.setToolTip("这个目录没有读取权限")
            item.setData(Qt.ItemDataRole.UserRole, tm)
            if not tm.get("blocked"):
                item.setToolTip(tm["name"])
            self.grid.addItem(item)

    # 文件夹右下角加一枚红色禁用标记；文件没有这个状态
    def _kindIcon(self, isDir, blocked=False, size=20):
        if isDir:
            return folderIcon(self.style(), size, blocked)
        return self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)

    # 双击列表
    def _onTableActivate(self, row, _col):
        item = self.table.item(row, 0)
        if item is None:
            return
        tm = item.data(Qt.ItemDataRole.UserRole)
        if tm is not None:
            self.sigActivated.emit(tm)

    # 双击项目
    def _onGridActivate(self, item):
        tm = item.data(Qt.ItemDataRole.UserRole)
        if tm is not None:
            self.sigActivated.emit(tm)

    # 右键交给主窗口填菜单，这里只负责弹出
    def _popup(self, widget, pos):
        idx = widget.indexAt(pos)
        if idx.isValid() and not widget.selectionModel().isSelected(idx):
            widget.setCurrentIndex(idx)
        menu = QMenu(self)
        self.sigMenu.emit(menu, widget)
        menu.exec(widget.viewport().mapToGlobal(pos))

    # 拖出时带上当前选中
    def _emitDragOut(self):
        tlTm = self.selectedEntries()
        if tlTm:
            self.sigDragOut.emit(tlTm)


IOS_MIME = "application/x-iosfiletool"
IOS_WINDOWS_MIME = 'application/x-qt-windows-mime;value="application/x-iosfiletool"'


# Qt 自己的拖动与原生 OLE 拖动采用两种等价格式名称
def _phoneFormat(mime):
    if mime.hasFormat(IOS_MIME):
        return IOS_MIME
    return IOS_WINDOWS_MIME if mime.hasFormat(IOS_WINDOWS_MIME) else ""



# 只接收本地文件和本工具的手机条目，网页链接不能导入
def _canDropHere(mime):
    return bool(_phoneFormat(mime)) or any(url.isLocalFile() for url in mime.urls())


# 文件夹落点不能被标成无权限；手机内部移动必须有明确目录
def _canDropAt(view, event):
    mime = event.mimeData()
    if not _canDropHere(mime):
        return False
    tm = _entryAt(view, event.position().toPoint())
    if tm and tm.get("isDir") and tm.get("blocked"):
        return False
    return bool(_dirAt(view, event.position().toPoint())) if _phoneFormat(mime) else True


# 电脑和手机间始终复制，防止 Shift 拖入导致资源管理器删除电脑原件
def _acceptDrop(event):
    event.setDropAction(Qt.DropAction.CopyAction)
    event.accept()


# 放下时再次校验；失败不向系统报告成功
def _takeDrop(view, event):
    if not _canDropAt(view, event):
        return False
    mime = event.mimeData()
    dest = _dirAt(view, event.position().toPoint())
    if _phoneFormat(mime):
        try:
            tlTm = json.loads(bytes(mime.data(_phoneFormat(mime))).decode("utf-8"))
            if not isinstance(tlTm, list) or not tlTm or any(
                not isinstance(tm, dict) or not isinstance(tm.get("remotePath"), str)
                or not isinstance(tm.get("name"), str) for tm in tlTm
            ):
                return False
        except (ValueError, UnicodeError):
            logging.getLogger("IosFileTool").exception("无法解析手机拖放数据")
            return False
        view.sigMove.emit(tlTm, dest)
        return True
    tlPath = _urlsToPaths(mime)
    if not tlPath:
        return False
    logging.getLogger("IosFileTool").info("收到拖入 paths=%s target=%s", tlPath, dest or "当前目录")
    view.sigDropIn.emit(tlPath, dest)
    return True


# 光标下的手机文件夹。文件和打不开的目录不算目标
def _dirAt(view, pos):
    idx = view.indexAt(pos)
    if not idx.isValid():
        return ""
    tm = _entryAt(view, pos)
    if not tm or not tm.get("isDir") or tm.get("blocked"):
        return ""
    return tm.get("remotePath") or ""


# 树节点的主角色存路径，完整条目另存；列表和项目直接存条目
def _entryAt(view, pos):
    idx = view.indexAt(pos)
    if not idx.isValid():
        return None
    tm = idx.data(Qt.ItemDataRole.UserRole)
    if isinstance(tm, str):
        return idx.data(Qt.ItemDataRole.UserRole + 3)
    return tm


# 从拖放数据里取出本地路径
def _urlsToPaths(mime):
    tlPath = []
    for url in mime.urls():
        if url.isLocalFile():
            tlPath.append(url.toLocalFile())
    return tlPath


# 系统文件夹图标。打不开时在右下角盖一枚小红标
def folderIcon(style, size, blocked):
    folder = style.standardIcon(QStyle.StandardPixmap.SP_DirIcon).pixmap(size, size)
    if not blocked:
        return QIcon(folder)
    return QIcon(stampBlocked(folder))


# 在任意图标右下角盖一枚小红标
def stampBlocked(pixmap):
    size = pixmap.width()
    canvas = QPixmap(size, size)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.drawPixmap(0, 0, pixmap)
    badge = max(8, size // 2)
    x = size - badge
    y = size - badge
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(210, 45, 45))
    painter.drawEllipse(x, y, badge - 1, badge - 1)
    painter.setPen(QPen(QColor(255, 255, 255), max(1, badge // 7)))
    painter.drawLine(x + 2, y + badge // 2, x + badge - 3, y + badge // 2)
    painter.end()
    return canvas


# 字节数写成简短大小
def _formatSize(size):
    n = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            if unit == "B":
                return "%d B" % int(n)
            return "%.1f %s" % (n, unit)
        n /= 1024
    return "%d B" % int(size)
