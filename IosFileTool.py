# -*- coding: utf-8 -*-

import os
import logging
import posixpath
import sys

from PySide6.QtCore import QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.appTheme import applyDarkTheme
from src.constants import PROGRAM_TITLE_NAME, PROGRAM_VERSION
from src.deviceService import DeviceService
from src.diagnostics import configureLogging
from src.fileBrowser import FileBrowser, stampBlocked
from src.fileClipboard import FileClipboard
from src.logPanel import LogPanel
from src.pathNav import PathBar, PathTree
from src.transferQueue import TransferQueue
from src.virtualFileDrag import VirtualFileDrag


# 结果只在 finished 后交给界面，避免上一线程仍运行时启动下一步
class _JobThread(QThread):
    # 保存操作标签，异常日志可以定位具体请求
    def __init__(self, fn, label):
        super().__init__()
        self._fn = fn
        self.label = label
        self.result = None
        self.error = ""

    # 后台只处理设备，界面回调始终在主线程
    def run(self):
        try:
            self.result = self._fn()
        except Exception as ex:
            logging.getLogger("IosFileTool").exception("操作失败：%s", self.label)
            self.error = "%s: %s" % (type(ex).__name__, ex)


# 主界面：左边 App，右边当前目录，导入导出走当前选中项
class MainGui(QMainWindow):
    sigProgress = Signal(str)
    sigBrowse = Signal(str, object)
    sigBrowseStatus = Signal(str)

    # 建窗口并挂上设备服务
    def __init__(self):
        super().__init__()
        configureLogging()
        self.device = DeviceService()
        self.sigProgress.connect(self._showProgress)
        self.sigBrowse.connect(self._onBrowseOne)
        self.device.setProgressHook(self.sigProgress.emit)
        self._job = None
        self._tlPendingJob = []
        self._busy = False
        self._connected = False
        self._closing = False
        self._dragActive = False
        self._clipboardBusy = False
        self.clipboard = None
        self._tlTmApp = []
        self._tmIcon = {}
        self._bundleId = ""
        self._remoteDir = "/"
        self._documentsOnly = False
        self._phoneDrag = None
        self.queue = TransferQueue(self.device)
        self.queue.sigIdle.connect(self._onQueueIdle)
        self.queue.sigBusyChanged.connect(self._syncUi)
        self._buildWindow()
        self.clipboard = FileClipboard(self.queue, self)
        self.clipboard.sigActiveChanged.connect(self._setClipboardBusy)
        self.clipboard.sigMessage.connect(self._status)
        self.sigBrowseStatus.connect(self.browseStatus.setText)
        self.queue.sigMessage.connect(self._status)
        self._syncUi()

    # 窗口布局
    def _buildWindow(self):
        self.setWindowTitle("%s v%s" % (PROGRAM_TITLE_NAME, PROGRAM_VERSION))
        self.resize(1100, 720)
        self.setMinimumSize(900, 560)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        top = QHBoxLayout()
        self.refreshBtn = QPushButton("刷新设备")
        self.refreshBtn.clicked.connect(self.onRefresh)
        self.deviceCombo = QComboBox()
        self.deviceCombo.setMinimumWidth(280)
        self.connectBtn = QPushButton("连接")
        self.connectBtn.clicked.connect(self.onConnect)
        top.addWidget(self.refreshBtn)
        top.addWidget(self.deviceCombo, 1)
        top.addWidget(self.connectBtn)
        top.addWidget(self.queue)
        root.addLayout(top)

        body = QSplitter()
        body.setChildrenCollapsible(False)
        body.addWidget(self._buildAppPane())
        self.pathTree = PathTree()
        self.pathTree.sigOpen.connect(self._onTreeOpen)
        self.pathTree.sigExpand.connect(self._onTreeExpand)
        body.addWidget(self.pathTree)
        body.addWidget(self._buildFilePane())
        body.setStretchFactor(0, 2)
        body.setStretchFactor(1, 1)
        body.setStretchFactor(2, 3)
        root.addWidget(body, 1)

        self.logPanel = LogPanel()
        root.addWidget(self.logPanel)

        self.setStatusBar(QStatusBar())
        self._status("先刷新设备。无线需要曾经用 USB 配对，并在 Apple 设备里打开 Wi-Fi 同步。")

    # 左侧 App 列表和过滤
    def _buildAppPane(self):
        pane = QWidget()
        lay = QVBoxLayout(pane)
        lay.setContentsMargins(0, 0, 0, 0)
        self.appSearch = QLineEdit()
        self.appSearch.setPlaceholderText("按名称或 Bundle ID 过滤")
        self.appSearch.textChanged.connect(self._fillAppTable)
        lay.addWidget(self.appSearch)

        row = QHBoxLayout()
        self.iconOnlyCheck = QCheckBox("只显示有图标")
        self.iconOnlyCheck.setChecked(True)
        self.iconOnlyCheck.toggled.connect(self._fillAppTable)
        self.showSystemCheck = QCheckBox("显示系统 App")
        self.showSystemCheck.setChecked(True)
        self.showSystemCheck.toggled.connect(self._fillAppTable)
        self.hideBlockedCheck = QCheckBox("不显示无法打开的 App")
        self.hideBlockedCheck.toggled.connect(self._fillAppTable)
        row.addWidget(self.iconOnlyCheck)
        row.addWidget(self.showSystemCheck)
        row.addWidget(self.hideBlockedCheck)
        row.addStretch(1)
        lay.addLayout(row)

        self.browseStatus = QLabel("访问检测：未连接")
        lay.addWidget(self.browseStatus)
        self.appTable = QTableWidget(0, 4)
        self.appTable.setHorizontalHeaderLabels(["名称", "Bundle ID", "类型", "访问"])
        self.appTable.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.appTable.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.appTable.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.appTable.setIconSize(QSize(24, 24))
        self.appTable.verticalHeader().setDefaultSectionSize(32)
        self.appTable.verticalHeader().setVisible(False)
        self.appTable.horizontalHeader().setStretchLastSection(True)
        self.appTable.cellDoubleClicked.connect(self.onOpenApp)
        self.appTable.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.appTable.customContextMenuRequested.connect(self._popupAppMenu)
        lay.addWidget(self.appTable)

        self.openBtn = QPushButton("打开选中 App")
        self.openBtn.clicked.connect(self.onOpenApp)
        lay.addWidget(self.openBtn)
        return pane

    # 右侧目录和导入导出
    def _buildFilePane(self):
        pane = QWidget()
        lay = QVBoxLayout(pane)
        lay.setContentsMargins(0, 0, 0, 0)
        self.pathBar = PathBar()
        self.pathBar.sigJump.connect(self._loadDir)
        lay.addWidget(self.pathBar)

        row = QHBoxLayout()
        self.upBtn = QPushButton("上级")
        self.upBtn.clicked.connect(self.onUp)
        self.refreshDirBtn = QPushButton("刷新")
        self.refreshDirBtn.clicked.connect(self.onRefreshDir)
        self.importFileBtn = QPushButton("导入文件")
        self.importFileBtn.clicked.connect(self.onImportFile)
        self.importDirBtn = QPushButton("导入文件夹")
        self.importDirBtn.clicked.connect(self.onImportDir)
        self.exportBtn = QPushButton("导出选中")
        self.exportBtn.clicked.connect(self.onExport)
        self.deleteBtn = QPushButton("删除选中")
        self.deleteBtn.clicked.connect(self.onDelete)
        row.addWidget(self.upBtn)
        row.addWidget(self.refreshDirBtn)
        row.addWidget(self.importFileBtn)
        row.addWidget(self.importDirBtn)
        row.addWidget(self.exportBtn)
        row.addWidget(self.deleteBtn)
        row.addStretch(1)
        lay.addLayout(row)

        self.fileBrowser = FileBrowser()
        self.fileBrowser.sigActivated.connect(self._onFileActivated)
        self.fileBrowser.sigDropIn.connect(self._onDropIn)
        self.fileBrowser.sigMove.connect(self._onMove)
        self.fileBrowser.sigDragOut.connect(self._onDragOut)
        self.pathTree.sigDropIn.connect(self._onDropIn)
        self.pathTree.sigMove.connect(self._onMove)
        self.fileBrowser.sigMenu.connect(self._fillFileMenu)
        self.fileBrowser.sigDelete.connect(self.onDelete)
        self.fileBrowser.sigCopy.connect(self.onCopy)
        self.fileBrowser.sigCut.connect(self.onCut)
        self.fileBrowser.sigPaste.connect(self.onPaste)
        lay.addWidget(self.fileBrowser, 1)
        return pane

    # 刷新 USB 和无线设备，只有一条时直接连接
    def onRefresh(self):
        if not self._canChangeContext():
            return
        self._startJob(self.device.listDevice, self._onDeviceListed, "刷新设备")

    # 连接下拉里选中的那条，USB 或 Wi-Fi
    def onConnect(self):
        if not self._canChangeContext():
            return
        tm = self.deviceCombo.currentData()
        if not tm:
            self._warn("没有可用设备")
            return
        serial = tm["serial"]
        connectionType = tm["connectionType"]
        self._connected = False
        self._resetApp()
        self._tlTmApp = []
        self._fillAppTable()
        self._startJob(lambda: self.device.connect(serial, connectionType), self._onConnected,
                       "连接 %s %s" % (connectionType, serial))

    # 打开表格里选中的 App
    def onOpenApp(self):
        if not self._canChangeContext():
            return
        bundleId = self._selectedBundleId()
        if not bundleId:
            self._warn("先选一个 App")
            return
        self._resetApp()
        self._startJob(lambda: self.device.openApp(bundleId), self._onAppOpened, "打开 App %s" % bundleId)

    # 双击目录进入
    def _onFileActivated(self, tm):
        if tm is None or not tm["isDir"]:
            return
        if tm.get("blocked"):
            self._status("打不开：%s" % tm["remotePath"])
            return
        self._loadDir(tm["remotePath"])

    # 重新读取当前目录，手机上刚改过的内容会出现在列表里
    def onRefreshDir(self):
        if not self._bundleId:
            self._warn("先打开一个 App")
            return
        self._loadDir(self._remoteDir)

    # 回到上一级，根目录不动
    def onUp(self):
        if not self._bundleId or self._remoteDir in ("", "/"):
            return
        parent = posixParent(self._remoteDir)
        self._loadDir(parent)

    # 把本地文件推进当前目录
    def onImportFile(self):
        if not self._bundleId:
            self._warn("先打开一个 App")
            return
        tlPath, _ = QFileDialog.getOpenFileNames(self, "导入到当前目录")
        if not tlPath:
            return
        self._enqueuePush(tlPath)

    # 把本地文件夹推进当前目录
    def onImportDir(self):
        if not self._bundleId:
            self._warn("先打开一个 App")
            return
        localDir = QFileDialog.getExistingDirectory(self, "导入文件夹到当前目录")
        if not localDir:
            return
        self._enqueuePush([localDir])

    # 把选中的文件或目录导出到本地文件夹
    def onExport(self):
        if not self._bundleId:
            self._warn("先打开一个 App")
            return
        tlTm = self._selectedEntries()
        if not tlTm:
            self._warn("先在右侧选中要导出的文件或文件夹")
            return
        localDir = QFileDialog.getExistingDirectory(self, "导出到文件夹")
        if not localDir:
            return
        self._enqueuePull(tlTm, localDir)

    # 复制手机选择到系统剪贴板，实际文件内容等接收方粘贴时读取
    def onCopy(self):
        self._copyFiles(False)

    # 手机内粘贴按移动处理，跨到 Windows 的粘贴仍按导出副本处理
    def onCut(self):
        self._copyFiles(True)

    # 搜索框中的文本快捷键不进入这里，只有文件视图或右键菜单调用
    def _copyFiles(self, cut):
        if not self._canTransfer():
            return
        tlTm = self._selectedEntries()
        if not tlTm:
            return
        try:
            self.clipboard.copy(tlTm, self.device.context(), cut)
            if cut:
                self._status("已剪切 %d 项：在本 App 内粘贴会移动，粘贴到 Windows 会导出副本。" % len(tlTm))
            else:
                self._status("已复制 %d 项，可在资源管理器或本 App 内粘贴。" % len(tlTm))
        except Exception as ex:
            logging.getLogger("IosFileTool").exception("复制手机文件到剪贴板失败")
            self._warn("复制失败：%s" % ex)

    # Windows 文件导入当前目录；手机文件在同一 App 内复制或移动
    def onPaste(self):
        if not self._canTransfer():
            return
        try:
            tmClip = self.clipboard.read()
            if tmClip is None:
                return
            if tmClip["kind"] == "local":
                self._enqueuePush(tmClip["paths"])
                return
            tm = tmClip["metadata"]
            tmContext = self.device.context()
            if tm.get("serial") != tmContext["serial"] or tm.get("bundleId") != tmContext["bundleId"]:
                self._warn("手机内粘贴请打开复制时的同一设备和 App。跨 App 可通过电脑导出、导入。")
                return
            self._refreshAfterQueue = True
            if tm.get("cut") and tmClip["owned"]:
                tlId = self.queue.enqueueMove(tm["entries"], self._remoteDir, True)
                self.clipboard.watchCut(tlId, tm["token"])
                self._status("已加入剪切粘贴队列")
            else:
                self.queue.enqueueCopy(tm["entries"], self._remoteDir)
                self._status("已加入复制粘贴队列；同名项自动生成副本名。")
        except Exception as ex:
            logging.getLogger("IosFileTool").exception("粘贴文件失败")
            self._warn("粘贴失败：%s" % ex)

    # 外部接收方尚在读取时，保持连接选择与删除入口锁定
    def _setClipboardBusy(self, busy):
        self._clipboardBusy = busy
        self._syncUi()

    # 删除前再确认一次，文件夹会连内容一起删
    def onDelete(self):
        if self.queue.isBusy() or self._busy or self._dragActive or self._clipboardBusy:
            self._status("请等当前操作完成后再删除。")
            return
        if not self._bundleId:
            self._warn("先打开一个 App")
            return
        tlTm = self._selectedEntries()
        if not tlTm:
            self._warn("先在右侧选中要删除的文件或文件夹")
            return
        tlName = []
        hasDir = False
        for tm in tlTm:
            tlName.append(tm["name"])
            if tm["isDir"]:
                hasDir = True
        text = "确定删除手机上的这 %d 项？\n\n%s" % (len(tlName), "\n".join(tlName))
        if hasDir:
            text += "\n\n其中包含文件夹，里面的文件会一起删除，不能恢复。"
        answer = QMessageBox.question(
            self,
            PROGRAM_TITLE_NAME,
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        remoteDir = self._remoteDir
        self._startJob(lambda: self.device.removeMany(tlTm), lambda _r: self._loadDir(remoteDir))

    # 先停止队列，等后台操作退出后再断开，避免 QThread 仍在运行就销毁
    def closeEvent(self, event):
        if getattr(self, "_closeReady", False):
            event.accept()
            return
        event.ignore()
        if self._closing:
            return
        self._closing = True
        if self.clipboard is not None:
            self.clipboard.close()
        if self._phoneDrag is not None:
            self._phoneDrag.cancel()
        self._tlPendingJob.clear()
        self.queue.shutdown()
        self._syncUi()
        self._status("正在结束后台任务并断开手机…")
        self._closeTimer = QTimer(self)
        self._closeTimer.timeout.connect(self._tryClose)
        self._closeTimer.start(100)

    # 不阻塞界面等待工作线程；最后一个断开任务同样在后台执行
    def _tryClose(self):
        if self._job is not None or self._dragActive or not self.queue.isStopped():
            return
        self._closeTimer.stop()
        self._startJob(self.device.close, self._finishClose, "断开设备", self._closeFailed)

    # 断开失败也保留错误记录，再结束本地窗口
    def _closeFailed(self, text):
        self._status("断开设备失败：%s" % text)
        self._finishClose(None)

    # 此时所有工作线程已退出，窗口可以安全销毁
    def _finishClose(self, _result):
        self._closeReady = True
        self.close()

    # 设备列表回来后填下拉框
    def _onDeviceListed(self, tlTmDev):
        self.deviceCombo.clear()
        hasWifi = False
        for tm in tlTmDev:
            link = "USB" if tm["connectionType"] == "USB" else "Wi-Fi"
            if tm["connectionType"] == "Network":
                hasWifi = True
            self.deviceCombo.addItem("%s  %s" % (link, tm["serial"]), tm)
        if not tlTmDev:
            self._connected = False
            self._resetApp()
            self._tlTmApp = []
            self._fillAppTable()
            self._status("没有设备。插上数据线，或先打开 Wi-Fi 同步后再刷新。")
            return
        if hasWifi:
            self._status("找到 %d 条连接，含 Wi-Fi。连上后会显示手机名。" % len(tlTmDev))
        else:
            self._status("找到 %d 条 USB。连上后会显示手机名。" % len(tlTmDev))
        if len(tlTmDev) == 1:
            QTimer.singleShot(0, self.onConnect)

    # 连上之后立刻拉 App 列表
    def _onConnected(self, tmDev):
        self._connected = True
        self._bundleId = ""
        self._remoteDir = "/"
        self.fileBrowser.clear()
        self.pathTree.clear()
        self.pathBar.setPath("/")
        self._renameDevice(tmDev)
        self._status("已连接 %s  %s  %s" % (tmDev["name"], tmDev["version"], tmDev["link"]))
        self._status("正在读取 App 列表…")
        self._startJob(self.device.listApp, self._onApps)

    # App 列表只存一份，过滤在界面上做
    def _onApps(self, tlTmApp):
        self._tlTmApp = tlTmApp
        self._fillAppTable()
        needIcon = False
        for tm in tlTmApp:
            if not tm["iconKnown"]:
                needIcon = True
                break
        if needIcon:
            self._status("正在读取图标，有图标的会排在前面。")
            self._startJob(lambda: self.device.fetchIcons(tlTmApp), self._onIcons)
            return
        self._onIcons(tlTmApp)

    # 图标到齐后按有没有图标重排
    def _onIcons(self, tlTmApp):
        tlTmApp.sort(key=lambda tm: (
            0 if tm["hasIcon"] else 1,
            0 if tm["appType"] == "User" else 1,
            tm["name"].lower(),
            tm["bundleId"],
        ))
        self._fillAppTable()
        self.device.startBrowseScan(tlTmApp, lambda bundleId, ok: self.sigBrowse.emit(bundleId, ok), self.sigBrowseStatus.emit)
        self._status("图标已读完，可以直接打开 App。后台继续标出打不开的 App。")

    # 打开成功后从根目录列文件
    def _onAppOpened(self, tmOpen):
        self._bundleId = tmOpen["bundleId"]
        self._documentsOnly = tmOpen["documentsOnly"]
        self._remoteDir = "/"
        self.fileBrowser.search.clear()
        mode = "仅 Documents" if self._documentsOnly else "整个容器"
        self._status("%s 已打开（%s）" % (self._bundleId, mode))
        self._loadDir("/")

    # 按搜索框和系统 App 开关重画左侧表
    def _fillAppTable(self):
        selectedId = self._selectedBundleId()
        key = self.appSearch.text().strip().lower()
        showSystem = self.showSystemCheck.isChecked()
        iconOnly = self.iconOnlyCheck.isChecked()
        hideBlocked = self.hideBlockedCheck.isChecked()
        tlTm = []
        for tm in self._tlTmApp:
            if iconOnly and tm["iconKnown"] and not tm["hasIcon"]:
                continue
            if hideBlocked and tm.get("browseKnown") and not tm.get("canBrowse"):
                continue
            if not showSystem and tm["appType"] != "User":
                continue
            if key and key not in tm["name"].lower() and key not in tm["bundleId"].lower():
                continue
            tlTm.append(tm)
        self.appTable.setRowCount(len(tlTm))
        for row, tm in enumerate(tlTm):
            self._setCell(self.appTable, row, 0, tm["name"], tm)
            icon = self._iconFor(tm)
            if icon is not None:
                self.appTable.item(row, 0).setIcon(icon)
            self._setCell(self.appTable, row, 1, tm["bundleId"], tm)
            self._setCell(self.appTable, row, 2, tm["appType"], tm)
            self._setCell(self.appTable, row, 3, self._browseText(tm), tm)
            self._paintBrowseRow(row, tm)
            if tm["bundleId"] == selectedId:
                self.appTable.setCurrentCell(row, 0)
        self.appTable.resizeColumnsToContents()

    # 请求的路径随回调保存，不使用可能被另一个请求改掉的共享路径
    def _loadDir(self, remoteDir):
        if not self._bundleId or self._closing or self._dragActive:
            return
        remoteDir = posixpath.normpath(remoteDir or "/")
        self._startJob(lambda: self.device.listDir(remoteDir),
                       lambda tl: self._onDir(remoteDir, tl), "读取目录 %s" % remoteDir,
                       lambda text: self._failDir(remoteDir, text))

    # 目录和树使用同一份结果；换目录时清掉上一目录的过滤词
    def _onDir(self, remoteDir, tlTmEntry):
        self._onBrowseOne(self._bundleId, True)
        if self.device.accessNote:
            self._documentsOnly = self.device.documentsOnly
        if remoteDir != self._remoteDir:
            self.fileBrowser.search.clear()
        self._remoteDir = remoteDir
        self.pathBar.setPath(remoteDir)
        self.fileBrowser.setEntries(tlTmEntry)
        if remoteDir == "/":
            self.pathTree.setRoots(tlTmEntry)
        else:
            self.pathTree.fillChildren(remoteDir, tlTmEntry)
        self.pathTree.selectPath(remoteDir)
        mode = self.device.accessNote or ("仅 Documents" if self._documentsOnly else "整个容器")
        self._status("%s  [%s] 共 %d 项" % (remoteDir, mode, len(tlTmEntry)))

    # 只针对失败的这个目录标记权限，不误标其它操作的目标
    def _failDir(self, remoteDir, text):
        if remoteDir == "/" and "没有对电脑开放文件目录" in text:
            self._onBrowseOne(self._bundleId, False)
        if "没有读取权限" in text or "PERM_DENIED" in text:
            self.fileBrowser.markBlocked(remoteDir)
            self.pathTree.markBlocked(remoteDir)
            self._status("打不开：%s；%s" % (remoteDir, text))
            return
        self._warn(text)

    # 点目录树打开该层
    def _onTreeOpen(self, remoteDir):
        self._loadDir(remoteDir)

    # 同时补多级目录时串行排队，每个请求保留自己的路径
    def _onTreeExpand(self, remoteDir):
        if not self._bundleId or self._closing or self._dragActive:
            return
        self._startJob(lambda: self.device.listDir(remoteDir),
                       lambda tl: self.pathTree.fillChildren(remoteDir, tl), "展开目录 %s" % remoteDir,
                       lambda text: self._failDir(remoteDir, text))

    # 下拉里的序列号后面补上手机名
    def _renameDevice(self, tmDev):
        for index in range(self.deviceCombo.count()):
            tm = self.deviceCombo.itemData(index)
            if not tm or tm.get("serial") != tmDev["serial"] or tm.get("connectionType") != self._linkType(tmDev):
                continue
            link = "USB" if tm.get("connectionType") == "USB" else "Wi-Fi"
            self.deviceCombo.setItemText(index, "%s  %s" % (link, tmDev["name"]))
        self.setWindowTitle("%s v%s - %s" % (PROGRAM_TITLE_NAME, PROGRAM_VERSION, tmDev["name"]))

    # 连接结果里的链路对应下拉项
    def _linkType(self, tmDev):
        if tmDev.get("link") == "USB":
            return "USB"
        return "Network"

    # 读图标时的文字进度
    def _showProgress(self, text):
        self.statusBar().showMessage(text)

    # 没图标的 App 也使用默认图标承载红标，避免无法打开状态消失
    def _iconFor(self, tm):
        blocked = bool(tm.get("browseKnown") and not tm.get("canBrowse"))
        key = tm["bundleId"] + ("#blocked" if blocked else "")
        if key in self._tmIcon:
            return self._tmIcon[key]
        pixmap = QPixmap()
        iconPath = tm.get("iconPath") or ""
        if not iconPath or not pixmap.load(iconPath):
            pixmap = self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon).pixmap(24, 24)
        if blocked:
            pixmap = stampBlocked(pixmap)
        icon = QIcon(pixmap)
        self._tmIcon[key] = icon
        return icon

    # 上次的负面结果可见，同时明确表示正在复查
    def _browseText(self, tm):
        if tm.get("browseKnown"):
            if tm.get("canBrowse"):
                return "可打开"
            return "无法打开（待复查）" if tm.get("browseStale") else "无法打开"
        return "待确认" if tm.get("browseUnknown") else "检测中"

    # 红标、文字、工具提示和行数据始终使用同一份结果
    def _paintBrowseRow(self, row, tm):
        blocked = bool(tm.get("browseKnown") and not tm.get("canBrowse"))
        tooltip = "上次检测无法打开，正在重新确认" if tm.get("browseStale") else "这个 App 没有对电脑开放目录" if blocked else ""
        self.appTable.item(row, 0).setIcon(self._iconFor(tm))
        for col in range(self.appTable.columnCount()):
            item = self.appTable.item(row, col)
            item.setData(Qt.ItemDataRole.UserRole, tm)
            item.setToolTip(tooltip)
        item = self.appTable.item(row, 3)
        item.setText(self._browseText(tm))
        item.setForeground(QColor(235, 100, 100) if blocked else QColor(210, 210, 210))

    # 一项异常只显示待确认；成功复查必须清掉旧红标，失败则立即显示
    def _onBrowseOne(self, bundleId, ok):
        tmHit = None
        for tm in self._tlTmApp:
            if tm["bundleId"] == bundleId:
                if ok is None:
                    tm["browseUnknown"] = True
                else:
                    tm["canBrowse"] = ok
                    tm["browseKnown"] = True
                    tm["browseStale"] = False
                    tm["browseUnknown"] = False
                tmHit = tm
                break
        if tmHit is None:
            return
        if ok is False:
            self.logPanel.info("打不开：%s" % (tmHit.get("name") or bundleId))
        if self.hideBlockedCheck.isChecked():
            self._fillAppTable()
            return
        for row in range(self.appTable.rowCount()):
            item = self.appTable.item(row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole)["bundleId"] == bundleId:
                self._paintBrowseRow(row, tmHit)
                return

    # 当前选中的 Bundle ID
    def _selectedBundleId(self):
        row = self.appTable.currentRow()
        if row < 0:
            return ""
        item = self.appTable.item(row, 0)
        if item is None:
            return ""
        tm = item.data(Qt.ItemDataRole.UserRole)
        if not tm:
            return ""
        return tm["bundleId"]

    # 右侧选中的文件项
    def _selectedEntries(self):
        return self.fileBrowser.selectedEntries()

    # 导入进指定目录，没指定就用当前目录。完成后刷新
    def _enqueuePush(self, tlPath, remoteDir=""):
        if not self._canTransfer():
            return
        self._refreshAfterQueue = True
        self.queue.enqueuePush(tlPath, remoteDir or self._remoteDir)
        self._status("已加入导入队列")

    # 导出到指定本地目录
    def _enqueuePull(self, tlTm, localDir):
        if not self._canTransfer():
            return
        tlJob = []
        for tm in tlTm:
            tlJob.append({
                "name": tm["name"],
                "remotePath": tm["remotePath"],
                "localPath": os.path.join(localDir, tm["name"]),
            })
        self.queue.enqueuePull(tlJob)
        self._status("已加入导出队列")

    # 从电脑拖进来。remoteDir 是落点文件夹，空则进当前目录
    def _onDropIn(self, tlPath, remoteDir=""):
        if not self._bundleId:
            self._warn("先打开一个 App")
            return
        self._enqueuePush(tlPath, remoteDir)

    # 手机里拖到另一个文件夹，使用同一容器的原生移动
    def _onMove(self, tlTm, remoteDir):
        if not remoteDir or not self._canTransfer():
            return
        tlKeep = []
        for tm in tlTm:
            if _sameOrInside(tm.get("remotePath") or "", remoteDir):
                continue
            parent = posixParent(tm.get("remotePath") or "")
            if parent == remoteDir:
                continue
            tlKeep.append(tm)
        if not tlKeep:
            return
        self._refreshAfterQueue = True
        self.queue.enqueueMove(tlKeep, remoteDir)
        self._status("已加入移动队列")

    # 一次手势直接启动虚拟拖放，拖动和悬停期间没有传输任务
    def _onDragOut(self, tlTm):
        if not tlTm or self._dragActive or not self._canTransfer():
            return
        session = VirtualFileDrag(self.device, self.queue, tlTm, self)
        self._phoneDrag = session
        session.sigFinished.connect(self._onPhoneDragFinished)
        self._dragActive = True
        self._syncUi()
        try:
            session.start()
        except Exception as ex:
            logging.getLogger("IosFileTool").exception("启动虚拟文件拖放失败")
            session.error = str(ex)
            session.finish(False)
            self._warn("拖出失败：%s" % ex)

    # Windows 报告完成后才放开 App 切换；下载结束并不等于落点写入完成
    def _onPhoneDragFinished(self, session, ok):
        if self._phoneDrag is not session:
            return
        self._phoneDrag = None
        self._dragActive = False
        self._syncUi()
        if session.error:
            self._status("拖出失败：%s" % session.error)
        elif ok:
            self._status("拖放已由目标接收")
        else:
            self._status("已取消拖放")
        self._onQueueIdle()
        session.deleteLater()

    # 队列空闲时刷新一次目录；等浏览任务结束后也会重试
    def _onQueueIdle(self):
        if self.clipboard is not None and not self._closing:
            self.clipboard.onQueueIdle()
        self._syncUi()
        if self._closing:
            return
        if self.queue.isBusy() or self._busy or self._dragActive:
            return
        if getattr(self, "_refreshAfterQueue", False) and self._bundleId:
            self._refreshAfterQueue = False
            self._loadDir(self._remoteDir)

    # 文件区右键
    def _fillFileMenu(self, menu, _widget):
        tlTm = self.fileBrowser.selectedEntries()
        actOpen = menu.addAction("打开")
        actOpen.setEnabled(len(tlTm) == 1 and tlTm[0]["isDir"])
        actOpen.triggered.connect(lambda: self._onFileActivated(tlTm[0]))
        menu.addSeparator()
        actCut = menu.addAction("剪切\tCtrl+X")
        actCut.setEnabled(bool(tlTm))
        actCut.triggered.connect(self.onCut)
        actCopy = menu.addAction("复制\tCtrl+C")
        actCopy.setEnabled(bool(tlTm))
        actCopy.triggered.connect(self.onCopy)
        actPaste = menu.addAction("粘贴\tCtrl+V")
        actPaste.setEnabled(bool(self._bundleId) and self.clipboard.canPaste())
        actPaste.triggered.connect(self.onPaste)
        menu.addSeparator()
        menu.addAction("导入文件…").triggered.connect(self.onImportFile)
        menu.addAction("导入文件夹…").triggered.connect(self.onImportDir)
        actExport = menu.addAction("导出到…")
        actExport.setEnabled(len(tlTm) > 0)
        actExport.triggered.connect(self.onExport)
        actDelete = menu.addAction("删除\tDel")
        actDelete.setEnabled(len(tlTm) > 0)
        actDelete.triggered.connect(self.onDelete)
        menu.addSeparator()
        menu.addAction("上级").triggered.connect(self.onUp)
        menu.addAction("刷新").triggered.connect(self.onRefreshDir)
        actCopy = menu.addAction("复制名称")
        actCopy.setEnabled(len(tlTm) > 0)
        actCopy.triggered.connect(lambda: self._copyText("\n".join([tm["name"] for tm in tlTm])))

    # 应用列表右键
    def _popupAppMenu(self, pos):
        menu = QMenu(self)
        bundleId = self._selectedBundleId()
        actOpen = menu.addAction("打开")
        actOpen.setEnabled(bool(bundleId))
        actOpen.triggered.connect(self.onOpenApp)
        actCopyId = menu.addAction("复制 Bundle ID")
        actCopyId.setEnabled(bool(bundleId))
        actCopyId.triggered.connect(lambda: self._copyText(bundleId))
        menu.addSeparator()
        menu.addAction("刷新设备").triggered.connect(self.onRefresh)
        menu.exec(self.appTable.viewport().mapToGlobal(pos))

    # 复制到剪贴板
    def _copyText(self, text):
        QGuiApplication.clipboard().setText(text)
        self._status("已复制")

    # 写单元格，并把整行数据挂在 UserRole 上
    def _setCell(self, table, row, col, text, tm):
        item = QTableWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, tm)
        table.setItem(row, col, item)

    # 所有后台请求串行；finished 后才释放线程并调用下一步
    def _startJob(self, fn, onOk, label="设备操作", onError=None):
        self._tlPendingJob.append((fn, onOk, label, onError))
        self._runNextJob()

    # 不再丢弃树展开或成功回调里发起的请求
    def _runNextJob(self):
        if self._job is not None:
            return
        if not self._tlPendingJob:
            self._setBusy(False)
            QTimer.singleShot(0, self._onQueueIdle)
            return
        fn, onOk, label, onError = self._tlPendingJob.pop(0)
        self._setBusy(True)
        job = _JobThread(fn, label)
        self._job = job
        job.finished.connect(self._finishJob)
        job.onOk = onOk
        job.onError = onError
        logging.getLogger("IosFileTool").info("开始：%s app=%s", label, self._bundleId)
        job.start()

    # Qt 主线程接收 finished；回调可以安全地继续排下一个操作
    def _finishJob(self):
        job = self._job
        self._job = None
        try:
            if job.error:
                (job.onError or self._warn)(job.error)
            elif not self._closing or job.label == "断开设备":
                job.onOk(job.result)
        except Exception as ex:
            logging.getLogger("IosFileTool").exception("界面回调失败：%s", job.label)
            self._warn("界面更新失败：%s" % ex)
        finally:
            job.deleteLater()
            self._runNextJob()

    # 切换连接或容器必须等传输全部结束，队列不能漂移到另一个 App
    def _canChangeContext(self):
        if self._busy or self.queue.isBusy() or self._dragActive or self._clipboardBusy or self._closing:
            self._status("请等当前操作或传输完成后再切换设备 / App。")
            return False
        return True

    # 拖入和菜单共用检查，后台切换容器时不能提交文件任务
    def _canTransfer(self):
        if not self._bundleId or self._busy or self._closing:
            self._status("请先打开 App 并等目录加载完成。")
            return False
        return True

    # 失败的连接或 App 切换也必须清掉旧目录，不能拿旧界面操作新通道
    def _resetApp(self):
        self._bundleId = ""
        self._remoteDir = "/"
        self.fileBrowser.clear()
        self.pathTree.clear()
        self.pathBar.setPath("/")

    # 保存通讯忙碌状态，由统一入口决定各控件是否可用
    def _setBusy(self, busy):
        self._busy = busy
        self._syncUi()
        if busy:
            self.statusBar().showMessage("正在跟手机通讯…")

    # 连接、浏览、传输三个状态统一控制按钮、双击、路径和拖入入口
    def _syncUi(self):
        busy = self._busy or self._closing
        queueBusy = self.queue.isBusy()
        self.device.setTransferBusy(queueBusy or self._clipboardBusy)
        canSwitch = not busy and not queueBusy and not self._dragActive and not self._clipboardBusy
        canBrowse = bool(self._bundleId) and not busy
        self.refreshBtn.setEnabled(canSwitch)
        self.connectBtn.setEnabled(canSwitch and self.deviceCombo.count() > 0)
        self.deviceCombo.setEnabled(canSwitch)
        self.appTable.setEnabled(canSwitch and self._connected)
        self.openBtn.setEnabled(canSwitch and self._connected)
        self.pathTree.setEnabled(canBrowse)
        self.pathBar.setEnabled(canBrowse and not self._dragActive)
        self.fileBrowser.setEnabled(canBrowse)
        for btn in (self.upBtn, self.refreshDirBtn, self.importFileBtn, self.importDirBtn,
                    self.exportBtn, self.deleteBtn):
            btn.setEnabled(canBrowse and not self._dragActive)
        self.upBtn.setEnabled(canBrowse and not self._dragActive and self._remoteDir != "/")
        self.deleteBtn.setEnabled(canBrowse and not self._dragActive and not queueBusy and not self._clipboardBusy)

    # 普通提示
    def _warn(self, text):
        self._status(text.split("\n", 1)[0])
        QMessageBox.warning(self, PROGRAM_TITLE_NAME, text)

    # 状态栏和底部日志写同一句。通讯中的短提示不进日志
    def _status(self, text):
        self.statusBar().showMessage(text)
        self.logPanel.info(text)


# 目标就是自己，或在自己里面
def _sameOrInside(remotePath, destDir):
    remotePath = (remotePath or "").rstrip("/")
    destDir = (destDir or "").rstrip("/")
    if not remotePath:
        return False
    if remotePath == destDir:
        return True
    return destDir.startswith(remotePath + "/")


# 手机路径的上一级，根保持为 /
def posixParent(remoteDir):
    parent = posixpath.dirname(remoteDir.rstrip("/"))
    if not parent:
        return "/"
    return parent


# 字节数写成简短大小
def formatSize(size):
    n = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            if unit == "B":
                return "%d B" % int(n)
            return "%.1f %s" % (n, unit)
        n /= 1024
    return "%d B" % int(size)


# 入口
def main():
    app = QApplication(sys.argv)
    applyDarkTheme(app)
    win = MainGui()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    root = os.path.dirname(os.path.abspath(__file__))
    if root not in sys.path:
        sys.path.insert(0, root)
    main()
