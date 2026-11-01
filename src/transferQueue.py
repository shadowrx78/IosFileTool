# -*- coding: utf-8 -*-

import logging
import threading
import time

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QWidget

from src.deviceService import Cancelled
from src.transferHistory import TransferHistory, itemPercent
from src.transferPopup import TransferButton, TransferPopup


# 传输队列：一次只跑一条，等待中的可以单独拿掉，正在跑的在下一块停下
class TransferQueue(QWidget):
    sigChanged = Signal(int)
    sigIdle = Signal()
    sigBusyChanged = Signal()
    sigMessage = Signal(str)

    # 队列自己起一个线程，按顺序调用设备
    def __init__(self, device):
        super().__init__()
        self.device = device
        self._tlItem = []
        self._nextId = 1
        self._batchStartId = 1
        self._lock = threading.Lock()
        self._stop = False
        self._build()
        self.sigChanged.connect(self._paintItem)
        self._thread = threading.Thread(target=self._pump, name="ios-queue", daemon=True)
        self._thread.start()

    # 主窗口只保留紧凑按钮；气泡和历史记录按需显示
    def _build(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.button = TransferButton(self)
        lay.addWidget(self.button)
        self.popup = TransferPopup(self)
        self.popup.sigCancel.connect(self.cancel)
        self.popup.sigClear.connect(self.clearCompleted)
        self.popup.sigShowAll.connect(self.showHistory)
        self.popup.sigVisibility.connect(self.button.setChecked)
        self.button.clicked.connect(self._togglePopup)
        self.history = None
        self._refreshTimer = QTimer(self)
        self._refreshTimer.setSingleShot(True)
        self._refreshTimer.setInterval(80)
        self._refreshTimer.timeout.connect(self._refreshViews)

    # 汇总当前批次，不让旧批次影响新批次的百分比；清除记录不改变后台结果
    def _viewSnapshot(self, includeAll=False):
        tlAll = []
        tlRunning = []
        tlRecent = []
        pending = completed = failed = historyCount = batchCount = 0
        progress = 0.0
        known = batchFailed = batchCancelled = False
        with self._lock:
            for tm in self._tlItem:
                active = tm["state"] in ("等待", "传输中", "正在中断")
                pending += int(active)
                if not tm["hidden"]:
                    historyCount += 1
                    completed += int(tm["state"] == "完成")
                    failed += int(tm["state"] == "失败")
                    if includeAll:
                        tlAll.append(dict(tm))
                    if tm["state"] in ("传输中", "正在中断"):
                        tlRunning.append(dict(tm))
                if tm["id"] >= self._batchStartId:
                    batchCount += 1
                    batchFailed |= tm["state"] == "失败"
                    batchCancelled |= tm["state"] == "已中断"
                    if not active:
                        progress += 1
                        known = True
                    elif tm["total"] > 0:
                        progress += itemPercent(tm) / 100
                        known = True
            for tm in reversed(self._tlItem):
                if not tm["hidden"] and tm["state"] not in ("传输中", "正在中断"):
                    tlRecent.append(dict(tm))
                    if len(tlRecent) >= 6:
                        break
        if pending:
            state = "running"
        elif not historyCount:
            state = "idle"
        elif batchFailed:
            state = "failed"
        elif batchCancelled:
            state = "cancelled"
        else:
            state = "done"
        percent = min(99, int(progress * 100 / batchCount)) if pending and known else None
        if not pending and batchCount:
            percent = 100
        tmSummary = {"state": state, "pending": pending, "percent": percent,
                     "historyCount": historyCount, "failedCount": failed, "completedCount": completed}
        return tmSummary, (tlRunning + tlRecent)[:6], tlAll

    # 汇总与气泡每秒最多更新约十二次，隐藏的历史窗口不重复构建表格
    def _refreshViews(self):
        self._refreshTimer.stop()
        tmSummary, tlRecent, _tlAll = self._viewSnapshot()
        self.button.setSummary(tmSummary)
        if self.popup.isVisible():
            self.popup.setItems(tlRecent, tmSummary)
        if self.history is not None and self.history.isVisible():
            self.history.setSummary(tmSummary)

    # 弹出前同步最新记录，外部点击或 Escape 由 Qt 自动收起
    def _togglePopup(self):
        if self.popup.isVisible():
            self.popup.hide()
            return
        tmSummary, tlRecent, _tlAll = self._viewSnapshot()
        self.button.setSummary(tmSummary)
        self.popup.setItems(tlRecent, tmSummary)
        self.popup.openAt(self.button)

    # 独立窗口复用同一份队列，关闭它不取消任何传输
    def showHistory(self):
        self.popup.hide()
        if self.history is None:
            self.history = TransferHistory(self.window())
            self.history.sigCancel.connect(self.cancel)
            self.history.sigClear.connect(self.clearCompleted)
            self.history.sigClearCompleted.connect(self.clearCompleted)
            self.table = self.history.table
            self.clearDoneBtn = self.history.clearDoneBtn
        tmSummary, _tlRecent, tlAll = self._viewSnapshot(True)
        self.history.setItems(tlAll, tmSummary)
        self.history.show()
        self.history.raise_()
        self.history.activateWindow()

    # 导入若干本地路径到当前手机目录
    def enqueuePush(self, tlPath, remoteDir):
        tlId = []
        with self._lock:
            for localPath in tlPath:
                tlId.append(self._addItem("导入", _baseName(localPath), localPath, "", remoteDir))
        for itemId in tlId:
            self.sigChanged.emit(itemId)
        self.sigBusyChanged.emit()
        return tlId

    # 同一容器内直接移动，保留原名，不经过本地临时目录
    def enqueueMove(self, tlTm, remoteDir, renameOnConflict=False):
        tlId = []
        with self._lock:
            for tm in tlTm:
                itemId = self._addItem("移动", tm["name"], "", tm["remotePath"], remoteDir)
                self._find(itemId)["renameOnConflict"] = renameOnConflict
                tlId.append(itemId)
        for itemId in tlId:
            self.sigChanged.emit(itemId)
        self.sigBusyChanged.emit()
        return tlId

    # 导出若干手机路径。localPath 由调用方算好
    def enqueuePull(self, tlTmJob, kind="导出", device=None):
        tlId = []
        with self._lock:
            for tm in tlTmJob:
                tlId.append(self._addItem(kind, tm["name"], tm["localPath"], tm["remotePath"], "", device))
        for itemId in tlId:
            self.sigChanged.emit(itemId)
        self.sigBusyChanged.emit()
        return tlId

    # 同一手机 App 内复制，目标同名时由设备服务生成副本名
    def enqueueCopy(self, tlTm, remoteDir):
        tlId = []
        with self._lock:
            for tm in tlTm:
                tlId.append(self._addItem("复制", tm["name"], "", tm["remotePath"], remoteDir))
        for itemId in tlId:
            self.sigChanged.emit(itemId)
        self.sigBusyChanged.emit()
        return tlId

    # 只移除界面记录，保留结果索引供尚在读取的虚拟文件引用，不删除实际文件
    def clearCompleted(self, itemId=None):
        tlsId = set()
        with self._lock:
            for tm in self._tlItem:
                if (itemId is None or tm["id"] == itemId) and tm["state"] == "完成" and not tm["hidden"]:
                    tm["hidden"] = True
                    tlsId.add(tm["id"])
        if self.history is not None:
            tmSummary, _tlRecent, tlAll = self._viewSnapshot(True)
            self.history.setItems(tlAll, tmSummary)
        self._refreshViews()
        return len(tlsId)

    # 等待中的直接标掉，正在传的通知设备
    def cancel(self, itemId):
        running = False
        device = self.device
        with self._lock:
            tm = self._find(itemId)
            if tm is None:
                return
            if tm["state"] == "等待":
                tm["cancelEvent"].set()
                tm["state"] = "已中断"
            elif tm["state"] == "传输中" and tm["kind"] != "移动":
                tm["cancelEvent"].set()
                tm["state"] = "正在中断"
                running = True
                device = tm["device"]
        if running:
            device.requestCancel()
        self.sigChanged.emit(itemId)
        self.sigBusyChanged.emit()
        if not self.isBusy():
            self.sigIdle.emit()

    # 这一批 id 是否都已经成功结束
    def allDone(self, tlId):
        with self._lock:
            for itemId in tlId:
                tm = self._find(itemId)
                if tm is None or tm["state"] != "完成":
                    return False
        return True

    # 按需拖出读取必须区分仍在等待与失败、中断，不能永远等 allDone
    def allFinished(self, tlId):
        with self._lock:
            return all(self._find(itemId)["state"] not in ("等待", "传输中", "正在中断") for itemId in tlId)

    # 停止取新任务；关闭窗口时等当前任务安全退出后再断开手机
    def shutdown(self):
        self._stop = True
        self.popup.hide()
        if self.history is not None:
            self.history.close()
        self._refreshTimer.stop()
        self.button._spinner.stop()
        tlsDevice = {self.device}
        with self._lock:
            for tm in self._tlItem:
                if tm["state"] in ("传输中", "正在中断"):
                    tlsDevice.add(tm["device"])
                tm["cancelEvent"].set()
                if tm["state"] == "等待":
                    tm["state"] = "已中断"
        for device in tlsDevice:
            device.requestCancel()

    # 检查后台线程是否已经退出，供窗口延后关闭
    def isStopped(self):
        return not self._thread.is_alive()

    # 成功项的本地路径，给拖出用
    def localPaths(self, tlId):
        tlPath = []
        with self._lock:
            for itemId in tlId:
                tm = self._find(itemId)
                if tm is not None and tm["state"] == "完成":
                    tlPath.append(tm["localPath"])
        return tlPath

    # 还有没有没做完的
    def isBusy(self):
        with self._lock:
            for tm in self._tlItem:
                if tm["state"] in ("等待", "传输中", "正在中断"):
                    return True
        return False

    # 只负责入列。信号必须等锁放开再发，否则界面线程会和这把锁卡死
    def _addItem(self, kind, name, localPath, remotePath, remoteDir, device=None):
        itemId = self._nextId
        self._nextId += 1
        if not any(tm["state"] in ("等待", "传输中", "正在中断") for tm in self._tlItem):
            self._batchStartId = itemId
        tm = {
            "id": itemId,
            "hidden": False,
            "device": device or self.device,
            "name": name,
            "kind": kind,
            "state": "等待",
            "done": 0,
            "total": 0,
            "speed": 0,
            "localPath": localPath,
            "remotePath": remotePath,
            "remoteDir": remoteDir,
            "error": "",
            "cancelEvent": threading.Event(),
        }
        self._tlItem.append(tm)
        return itemId

    # 按顺序取下一条等待项
    def _takeWaiting(self):
        with self._lock:
            for tm in self._tlItem:
                if tm["state"] == "等待":
                    tm["state"] = "传输中"
                    return tm
        return None

    # 找到某一条
    def _find(self, itemId):
        for tm in self._tlItem:
            if tm["id"] == itemId:
                return tm
        return None

    # 队列线程
    def _pump(self):
        while not self._stop:
            tm = self._takeWaiting()
            if tm is None:
                time.sleep(0.05)
                continue
            self.sigChanged.emit(tm["id"])
            self._runItem(tm)
            self.sigChanged.emit(tm["id"])
            self.sigBusyChanged.emit()
            self.sigIdle.emit()

    # 跑一条。中断和失败都留在这一行上
    def _runItem(self, tm):
        # 后台进度只更新数据并发送主线程通知
        def onProgress(done, total, speed, itemId=tm["id"]):
            with self._lock:
                cur = self._find(itemId)
                if cur is None or cur["state"] in ("已中断", "正在中断"):
                    return
                cur["done"] = done
                cur["total"] = total
                cur["speed"] = speed
            self.sigChanged.emit(itemId)

        logger = logging.getLogger("IosFileTool")
        logger.info("任务 #%s 开始 kind=%s local=%s remote=%s target=%s",
                    tm["id"], tm["kind"], tm["localPath"], tm["remotePath"], tm["remoteDir"])
        device = tm["device"]
        try:
            if tm["kind"] in ("导出", "拖出", "剪贴板导出"):
                device.pullOne(tm["remotePath"], tm["localPath"], onProgress, tm["cancelEvent"])
            elif tm["kind"] == "移动":
                if tm.get("renameOnConflict"):
                    device.moveOne(tm["remotePath"], tm["remoteDir"], True)
                else:
                    device.moveOne(tm["remotePath"], tm["remoteDir"])
            elif tm["kind"] == "复制":
                device.copyOne(tm["remotePath"], tm["remoteDir"], onProgress, tm["cancelEvent"])
            else:
                device.pushOne(tm["localPath"], tm["remoteDir"], onProgress, tm["cancelEvent"])
            with self._lock:
                if tm["state"] == "正在中断":
                    tm["state"] = "已中断"
                elif tm["state"] == "传输中":
                    tm["state"] = "完成"
                    tm["done"] = tm["total"]
        except Cancelled:
            with self._lock:
                tm["state"] = "已中断"
        except Exception as ex:
            logger.exception("任务 #%s 失败", tm["id"])
            with self._lock:
                if tm["state"] == "正在中断":
                    tm["state"] = "已中断"
                else:
                    tm["state"] = "失败"
                    tm["error"] = "%s" % ex

        logger.info("任务 #%s 结束 state=%s error=%s", tm["id"], tm["state"], tm["error"])
        self.sigMessage.emit("任务 #%s %s：%s%s" % (
            tm["id"], tm["state"], tm["name"], "；" + tm["error"] if tm["error"] else ""))

    # 只刷新已打开的历史行，按钮与气泡合并高频进度通知
    def _paintItem(self, itemId):
        if self._stop:
            return
        if self.history is not None and self.history.isVisible():
            with self._lock:
                tm = self._find(itemId)
                tmSnapshot = dict(tm) if tm is not None and not tm["hidden"] else None
            if tmSnapshot is not None:
                self.history.updateItem(tmSnapshot)
        if not self._refreshTimer.isActive():
            self._refreshTimer.start()

    # 全部记录窗口尚未创建时没有表格行
    def _rowOf(self, itemId):
        return -1 if self.history is None else self.history.rowOf(itemId)


# 本地或手机路径的最后一段
def _baseName(path):
    path = path.rstrip("\\/")
    slash = max(path.rfind("/"), path.rfind("\\"))
    if slash < 0:
        return path
    return path[slash + 1:]

