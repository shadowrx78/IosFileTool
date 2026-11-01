# -*- coding: utf-8 -*-

import json
import logging
import struct
import threading
import time
import uuid

import pythoncom
import pywintypes
import win32clipboard
import win32con
import winerror
from win32com.server.exception import COMException
from win32com.server.util import wrap
from win32com.shell import shellcon
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QGuiApplication

from src.deviceService import DeviceService
from src.virtualFileDrag import VirtualFileDrag, _FileDataObject, initializeOle, uninitializeOle

CLIPBOARD_FORMAT = "application/x-iosfiletool-clipboard"


# 系统剪贴板可能被其它程序短暂占用，只对这类错误做有限重试
def retryClipboard(fn):
    deadline = time.monotonic() + 0.6
    tlsRetry = {winerror.CLIPBRD_E_CANT_OPEN, winerror.CLIPBRD_E_CANT_CLOSE,
                winerror.CLIPBRD_E_CANT_EMPTY, winerror.CLIPBRD_E_CANT_SET, 5}
    while True:
        try:
            return fn()
        except (pythoncom.com_error, pywintypes.error) as ex:
            code = getattr(ex, "hresult", None) or getattr(ex, "winerror", None)
            if code not in tlsRetry or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


# 剪贴板来源单独持有连接，用户切换 App 后外部粘贴也不会读错容器
class PinnedClipboardSource:
    # 只保存来源身份；Ctrl+C 本身既不连接也不下载
    def __init__(self, tmContext):
        self._tmContext = dict(tmContext)
        self._device = None
        self._lock = threading.Lock()
        self._closed = False

    # 第一次请求目录元数据或文件内容时才打开固定来源
    def _requireDevice(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("这份手机文件剪贴板已失效")
            if self._device is None:
                device = DeviceService()
                try:
                    device.connect(self._tmContext["serial"], self._tmContext["connectionType"])
                    device.openApp(self._tmContext["bundleId"])
                except Exception:
                    device.dispose()
                    raise
                self._device = device
            return self._device

    # 目录只枚举元数据，复用虚拟文件的描述构造
    def describeDrag(self, tlTm):
        return self._requireDevice().describeDrag(tlTm)

    # 队列在后台读取来源，保持主窗口当前 App 不变
    def pullOne(self, remotePath, localPath, onProgress, cancelEvent=None):
        return self._requireDevice().pullOne(remotePath, localPath, onProgress, cancelEvent)

    # 独立队列项的中断只发给自己的连接
    def requestCancel(self):
        if self._device is not None:
            self._device.requestCancel()

    # 连接释放在后台执行，不能让剪贴板改变或关窗卡住界面
    def close(self):
        if self._closed:
            return
        self._closed = True
        # 等正在创建连接的线程退出临界区后再释放
        def release():
            with self._lock:
                device = self._device
                self._device = None
            if device is not None:
                try:
                    device.dispose()
                except Exception:
                    logging.getLogger("IosFileTool").exception("释放剪贴板来源连接失败")
        threading.Thread(target=release, name="ios-clipboard-close", daemon=True).start()


# 同一份复制可以粘贴多次；一次粘贴结束不销毁剪贴板数据对象
class _ClipboardSession(VirtualFileDrag):
    sigStarted = Signal(object)

    # 剪贴板没有鼠标拖动阶段，内容仍等实际读取才提交队列
    def __init__(self, source, queue, tlTmEntry, tmMetadata, parent):
        super().__init__(source, queue, tlTmEntry, parent)
        self.tmMetadata = tmMetadata
        self.dropped = True
        self.operations = 0
        self.syncReading = False
        self.retired = False

    # 接收方开始新一次粘贴，允许从上次的失败或中断恢复
    def beginOperation(self):
        if self.retired or self.finished:
            raise COMException(hresult=winerror.E_ABORT)
        self.operations += 1
        self.inOperation = True
        self.cancelled = False
        self.error = ""
        self.sigStarted.emit(self)

    # 不支持异步接口的接收端也可以惰性读取，完成由 Shell 反馈格式确认
    def localFile(self, index):
        if not self.inOperation:
            self.beginOperation()
            self.syncReading = True
        if index in self._tmLocal:
            _path, tlId = self._tmLocal[index]
            if self.queue.allFinished(tlId) and not self.queue.allDone(tlId):
                del self._tmLocal[index]
        return super().localFile(index)

    # 结束的是本次粘贴，缓存和对象继续供下一次 Ctrl+V 使用
    def finish(self, ok):
        if self.finished or self.operations <= 0:
            return
        self.operations -= 1
        self.inOperation = self.operations > 0
        self.syncReading = False
        self.sigFinished.emit(self, bool(ok))
        if self.retired and not self.inOperation:
            self.retire()

    # 取消后续读取；退休的剪贴板对象不能再次发起连接
    def retire(self):
        self.retired = True
        if not self.inOperation:
            self.finished = True
            self._comData = None
            self._data = None
            self.device.close()


# 在虚拟文件对象上附带手机内复制、剪切所需的来源和操作类型
class _ClipboardDataObject(_FileDataObject):
    # Windows 读取标准虚拟文件格式，工具内部读取私有元数据
    def __init__(self, session):
        super().__init__(session)
        self.metadata = win32clipboard.RegisterClipboardFormat(CLIPBOARD_FORMAT)
        self._tmExtra[self.metadata] = json.dumps(session.tmMetadata, ensure_ascii=False).encode("utf-8")
        self.performed = {win32clipboard.RegisterClipboardFormat("Performed DropEffect"),
                          win32clipboard.RegisterClipboardFormat("Paste Succeeded")}

    # 剪贴板格式枚举额外列出私有元数据，内容仍使用标准 IStream
    def EnumFormatEtc(self, direction):
        from win32com.server.util import NewEnum
        tlFormat = list(super().EnumFormatEtc(direction))
        tlFormat.append((self.metadata, None, pythoncom.DVASPECT_CONTENT, -1, pythoncom.TYMED_HGLOBAL))
        return NewEnum(tlFormat, iid=pythoncom.IID_IEnumFORMATETC)

    # 同步接收端使用结果反馈结束本次操作；异步接收端以 EndOperation 为准
    def SetData(self, fmt, medium, release=False):
        super().SetData(fmt, medium, release)
        if fmt[0] in self.performed and self.session.syncReading and medium.tymed == pythoncom.TYMED_HGLOBAL:
            effect = struct.unpack_from("<I", medium.data)[0]
            self.session.finish(bool(effect & shellcon.DROPEFFECT_COPY))


# Windows 和手机文件列表共用的一份系统剪贴板
class FileClipboard(QObject):
    sigActiveChanged = Signal(bool)
    sigMessage = Signal(str)

    # 监听所有者改变，释放已被替换且没有正在读取的旧来源
    def __init__(self, queue, parent=None, sourceFactory=None):
        super().__init__(parent)
        initializeOle()
        self.queue = queue
        self._sourceFactory = sourceFactory or PinnedClipboardSource
        self._current = None
        self._tlSession = []
        self._tlCut = []
        self._closed = False
        self._format = win32clipboard.RegisterClipboardFormat(CLIPBOARD_FORMAT)
        self._qtClipboard = QGuiApplication.clipboard()
        self._qtClipboard.dataChanged.connect(self._clipboardChanged)

    # 仅发布描述对象；Ctrl+C 不读取手机字节，也不创建队列任务
    def copy(self, tlTmEntry, tmContext, cut=False):
        tmMetadata = dict(tmContext, version=1, token=uuid.uuid4().hex,
                          cut=bool(cut), entries=[dict(tm) for tm in tlTmEntry])
        source = self._sourceFactory(tmContext)
        session = _ClipboardSession(source, self.queue, tlTmEntry, tmMetadata, self)
        session._data = _ClipboardDataObject(session)
        session._comData = wrap(session._data, pythoncom.IID_IDataObject)
        session.sigStarted.connect(self._operationStarted)
        session.sigFinished.connect(self._operationFinished)
        self._tlSession.append(session)
        previous = self._current
        self._current = session
        try:
            retryClipboard(lambda: pythoncom.OleSetClipboard(session._comData))
        except Exception:
            self._current = previous
            self._tlSession.remove(session)
            session.retire()
            raise
        self._collect()
        return session

    # 只查询格式，不读取内容；普通文本不能粘贴成手机文件
    def canPaste(self):
        return bool(win32clipboard.IsClipboardFormatAvailable(self._format)
                    or win32clipboard.IsClipboardFormatAvailable(win32con.CF_HDROP))

    # 手机内返回来源元数据；电脑文件只读取 CF_HDROP 路径，不改动电脑原件
    def read(self):
        if win32clipboard.IsClipboardFormatAvailable(self._format):
            data = retryClipboard(pythoncom.OleGetClipboard)
            medium = data.GetData((self._format, None, 1, -1, pythoncom.TYMED_HGLOBAL))
            tm = json.loads(medium.data.decode("utf-8").rstrip("\0"))
            if (not isinstance(tm, dict) or tm.get("version") != 1 or not isinstance(tm.get("entries"), list)
                    or not tm["entries"] or any(not isinstance(entry, dict) or not isinstance(entry.get("remotePath"), str)
                                               or not isinstance(entry.get("name"), str) for entry in tm["entries"])):
                raise RuntimeError("剪贴板中的手机文件信息无效")
            return {"kind": "phone", "metadata": tm, "owned": self._ownsToken(tm.get("token"))}
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_HDROP):
            retryClipboard(win32clipboard.OpenClipboard)
            try:
                tlPath = list(win32clipboard.GetClipboardData(win32con.CF_HDROP))
            finally:
                win32clipboard.CloseClipboard()
            return {"kind": "local", "paths": tlPath}
        return None

    # 只有自己的剪切在全部移动成功后才清空，失败可以重试；不影响后来复制的新内容
    def watchCut(self, tlId, token):
        self._tlCut.append((list(tlId), token))

    # 队列进度不依赖可见记录，清空已完成不会破坏剪贴板结果判断
    def onQueueIdle(self):
        tlKeep = []
        for tlId, token in self._tlCut:
            if not self.queue.allFinished(tlId):
                tlKeep.append((tlId, token))
            elif self.queue.allDone(tlId) and self._ownsToken(token):
                retryClipboard(lambda: pythoncom.OleSetClipboard(None))
                self._current = None
        self._tlCut = tlKeep
        self._collect()

    # 关闭工具只撤销自己的延迟文件剪贴板，不强制下载来固化，也不清空别的软件剪贴板
    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._current is not None and self._isCurrent(self._current):
                retryClipboard(lambda: pythoncom.OleSetClipboard(None))
        except pythoncom.com_error:
            logging.getLogger("IosFileTool").exception("关闭时撤销文件剪贴板失败")
        self._current = None
        for session in self._tlSession:
            session.cancel()
            session.retire()
        self._tlSession.clear()
        self.sigActiveChanged.emit(False)
        self._qtClipboard.dataChanged.disconnect(self._clipboardChanged)
        uninitializeOle()

    # 数据改变信号可能在 OLE 调用内部触发，稍后检查所有权以免重入
    def _clipboardChanged(self):
        if not self._closed:
            QTimer.singleShot(0, self._collect)

    # 原生所有权检查不读取用户剪贴板的内容
    def _isCurrent(self, session):
        return session._comData is not None and pythoncom.OleIsCurrentClipboard(session._comData)

    # 剪切操作必须来自本工具仍持有的同一份剪贴板
    def _ownsToken(self, token):
        return (self._current is not None and self._current.tmMetadata["token"] == token
                and self._isCurrent(self._current))

    # 当前复制与仍被外部读取的旧复制分别保留，避免新 Ctrl+C 中断先前粘贴
    def _collect(self):
        if self._closed:
            return
        if self._current is not None and not self._isCurrent(self._current):
            self._current = None
        tlKeep = []
        for session in self._tlSession:
            if session is self._current or session.inOperation:
                tlKeep.append(session)
            else:
                session.retire()
        self._tlSession = tlKeep
        self.sigActiveChanged.emit(any(session.inOperation for session in self._tlSession))

    # 首次外部粘贴开始后，界面在传输结束前锁住设备切换
    def _operationStarted(self, session):
        self.sigActiveChanged.emit(True)

    # 一次外部粘贴完成后仍保留可重复粘贴的当前复制
    def _operationFinished(self, session, ok):
        self.sigMessage.emit("剪贴板导出完成" if ok else "剪贴板导出失败或已中断：%s" % session.error)
        self._collect()
