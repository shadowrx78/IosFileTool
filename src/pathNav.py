# -*- coding: utf-8 -*-

from PySide6.QtCore import QSignalBlocker, QSize, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QWidget

from src.fileBrowser import _acceptDrop, _canDropHere, _takeDrop, folderIcon


# 像资源管理器那样可点击的路径
class PathBar(QWidget):
    sigJump = Signal(str)

    # 一条会换行的按钮路径
    def __init__(self):
        super().__init__()
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.addStretch(1)

    # 按 / 拆开。根按钮回到 App 根目录
    def setPath(self, remoteDir):
        while self._lay.count() > 1:
            item = self._lay.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        remoteDir = remoteDir or "/"
        title = QLabel("路径")
        self._lay.insertWidget(self._lay.count() - 1, title)
        self._addButton("根", "/")
        if remoteDir not in ("", "/"):
            acc = ""
            for part in remoteDir.strip("/").split("/"):
                acc = acc + "/" + part
                self._addSep()
                self._addButton(part, acc)

    # 插在最后的弹簧前面
    def _addButton(self, text, path):
        btn = QPushButton(text)
        btn.setFlat(True)
        btn.clicked.connect(lambda _c=False, path=path: self.sigJump.emit(path))
        self._lay.insertWidget(self._lay.count() - 1, btn)

    # 路径段之间的分隔
    def _addSep(self):
        sep = QLabel("\\")
        self._lay.insertWidget(self._lay.count() - 1, sep)


# 当前 App 的目录树。展开时再向手机要子目录
class PathTree(QTreeWidget):
    sigOpen = Signal(str)
    sigExpand = Signal(str)
    sigMove = Signal(list, str)
    sigDropIn = Signal(list, str)

    # 隐藏表头，点一下就打开该目录
    def __init__(self):
        super().__init__()
        self.setHeaderHidden(True)
        self.setMinimumWidth(180)
        self.setStyleSheet(
            "QTreeWidget { background-color: #1a1a1a; alternate-background-color: #242424;"
            " border: 1px solid #666666; }"
            "QTreeWidget::item { padding: 2px 2px; }"
            "QTreeWidget::item:selected { background-color: #1565c0; }"
        )
        self.setAlternatingRowColors(True)
        self.setIconSize(QSize(18, 18))
        self.itemClicked.connect(self._onClick)
        self.itemExpanded.connect(self._onExpand)
        self._filling = False
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)

    # 换成这个 App 的顶层
    def setRoots(self, tlTm):
        self._filling = True
        self.clear()
        root = self._makeItem({"name": "App 根目录", "remotePath": "/", "isDir": True})
        root.setData(0, Qt.ItemDataRole.UserRole + 2, True)
        self.addTopLevelItem(root)
        for tm in tlTm:
            if tm["isDir"]:
                root.addChild(self._makeItem(tm))
        root.setExpanded(True)
        self._filling = False

    # 展开后填上真实子项
    def fillChildren(self, remoteDir, tlTm):
        item = self._find(remoteDir)
        if item is None:
            return
        blocker = QSignalBlocker(self)
        self._filling = True
        selectedPath = self.currentItem().data(0, Qt.ItemDataRole.UserRole) if self.currentItem() else ""
        item.takeChildren()
        item.setData(0, Qt.ItemDataRole.UserRole + 2, True)
        for tm in tlTm:
            if not tm.get("isDir"):
                continue
            item.addChild(self._makeItem(tm))
        policy = (QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator if item.childCount()
                  else QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicator)
        item.setChildIndicatorPolicy(policy)
        self._filling = False
        if selectedPath:
            selected = self._find(selectedPath)
            if selected is None:
                selected = self._ensurePath(selectedPath)
            self.setCurrentItem(selected)
        del blocker

    # 跟着文件区当前路径选中，缺的层级先补占位
    def selectPath(self, remoteDir):
        if not remoteDir:
            remoteDir = "/"
        item = self._find(remoteDir)
        if item is None:
            item = self._ensurePath(remoteDir)
        if item is None:
            return
        self._filling = True
        tlNeed = []
        parent = item.parent()
        while parent is not None:
            parent.setExpanded(True)
            if not parent.data(0, Qt.ItemDataRole.UserRole + 2):
                tlNeed.append(parent)
            parent = parent.parent()
        self._filling = False
        self.setCurrentItem(item)
        self.scrollToItem(item)
        for parent in tlNeed:
            path = parent.data(0, Qt.ItemDataRole.UserRole)
            if path:
                self.sigExpand.emit(path)

    # 事后发现打不开时，改掉已有节点的文字
    def markBlocked(self, remoteDir):
        item = self._find(remoteDir)
        if item is None:
            return
        item.setIcon(0, folderIcon(self.style(), 18, True))
        item.setData(0, Qt.ItemDataRole.UserRole + 1, True)
        tm = item.data(0, Qt.ItemDataRole.UserRole + 3)
        tm["blocked"] = True
        item.setData(0, Qt.ItemDataRole.UserRole + 3, tm)
        item.setToolTip(0, "这个目录没有读取权限")
        item.takeChildren()

    # 目录节点。打不开的不再挂子节点
    def _makeItem(self, tm):
        item = QTreeWidgetItem([tm["name"]])
        item.setIcon(0, folderIcon(self.style(), 18, bool(tm.get("blocked"))))
        item.setData(0, Qt.ItemDataRole.UserRole, tm["remotePath"])
        item.setData(0, Qt.ItemDataRole.UserRole + 3, tm)
        item.setData(0, Qt.ItemDataRole.UserRole + 1, bool(tm.get("blocked")))
        if tm.get("blocked"):
            item.setToolTip(0, "这个目录没有读取权限")
        if tm.get("isDir") and not tm.get("blocked"):
            item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        return item

    # 进入树时只校验格式，让用户能从空白移到目录上
    def dragEnterEvent(self, event):
        if _canDropHere(event.mimeData()):
            _acceptDrop(event)
        else:
            event.ignore()

    # 只有可读目录才显示允许放下
    def dragMoveEvent(self, event):
        if self._dropDir(event):
            _acceptDrop(event)
        else:
            event.ignore()

    # 与文件视图使用相同的格式解析和复制规则
    def dropEvent(self, event):
        if self._dropDir(event) and _takeDrop(self, event):
            _acceptDrop(event)
        else:
            event.ignore()

    # Qt 的落点坐标已相对视口，不再重复偏移
    def _dropDir(self, event):
        if not _canDropHere(event.mimeData()):
            return ""
        item = self.itemAt(event.position().toPoint())
        if item is None or item.data(0, Qt.ItemDataRole.UserRole + 1):
            return ""
        return item.data(0, Qt.ItemDataRole.UserRole) or ""

    # 点目录就让文件区跳过去。打不开的只选中
    def _onClick(self, item, _col):
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if not path or item.data(0, Qt.ItemDataRole.UserRole + 1):
            return
        self.sigOpen.emit(path)

    # 第一次展开才去读子目录
    def _onExpand(self, item):
        if self._filling:
            return
        if item.data(0, Qt.ItemDataRole.UserRole + 2):
            return
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if path:
            self.sigExpand.emit(path)

    # 按路径找节点
    def _find(self, remoteDir):
        tl = self.findItems("*", Qt.MatchFlag.MatchWildcard | Qt.MatchFlag.MatchRecursive)
        for item in tl:
            if item.data(0, Qt.ItemDataRole.UserRole) == remoteDir:
                return item
        return None

    # 父级还没展开时，先把这一串路径补出来，子项等展开再读
    def _ensurePath(self, remoteDir):
        tlPart = remoteDir.strip("/").split("/")
        acc = ""
        parent = self._find("/")
        found = None
        for part in tlPart:
            acc = "/" + part if not acc else acc + "/" + part
            found = self._find(acc)
            if found is None:
                tm = {"name": part, "remotePath": acc, "isDir": True, "blocked": False}
                item = self._makeItem(tm)
                if parent is None:
                    self.addTopLevelItem(item)
                else:
                    parent.addChild(item)
                found = item
            parent = found
        return found
