# -*- coding: utf-8 -*-

import json
import ctypes
import logging
import os
import struct
import tempfile
import threading
from datetime import datetime, timezone

import pythoncom
import win32clipboard
import win32con
import win32gui
import winerror
from win32com.server.exception import COMException
from win32com.server.util import NewEnum, wrap
from win32com.shell import shell, shellcon
from PySide6.QtCore import QCoreApplication, QEventLoop, QObject, QTimer, Signal

IOS_FORMAT = "application/x-iosfiletool"
FILE_TIME_ZERO = datetime(1601, 1, 1, tzinfo=timezone.utc)


# 自己的每次 OLE 初始化都需要对称计数，不能依赖 pythoncom 的一次性初始化状态
def initializeOle():
    fn = ctypes.windll.ole32.OleInitialize
    fn.argtypes = [ctypes.c_void_p]
    fn.restype = ctypes.c_long
    result = fn(None)
    if result < 0:
        raise RuntimeError("初始化 Windows 文件剪贴板失败：0x%08X" % (result & 0xFFFFFFFF))


# 归还当前对象自己申请的 OLE 引用，不影响 Qt 或其它 COM 使用方
def uninitializeOle():
    fn = ctypes.windll.ole32.OleUninitialize
    fn.argtypes = []
    fn.restype = None
    fn()


# 把文件名、属性交给 Windows；实际内容只在落点确认后按需提供
class VirtualFileDrag(QObject):
    sigFinished = Signal(object, bool)

    # 构造阶段不创建临时目录、不访问手机，也不提交传输任务
    def __init__(self, device, queue, tlTmEntry, parent=None):
        super().__init__(parent)
        self.device = device
        self.queue = queue
        self.tlTmEntry = [dict(tm) for tm in tlTmEntry]
        self.tlTmManifest = None
        self._tmLocal = {}
        self._tlId = []
        self._folder = ""
        self.dropped = False
        self.cancelled = False
        self.finished = False
        self.asyncMode = True
        self.inOperation = False
        self.inDrag = False
        self.error = ""
        self._data = None
        self._comData = None

    # 主线程运行 OLE 拖放；异步目标可以在返回后继续读取内容
    def start(self):
        if self.cancelled:
            self.finish(False)
            return
        initializeOle()
        self.inDrag = True
        try:
            self._data = _FileDataObject(self)
            self._comData = wrap(self._data, pythoncom.IID_IDataObject)
            source = wrap(_DropSource(self), pythoncom.IID_IDropSource)
            if win32gui.GetCapture():
                win32gui.ReleaseCapture()
            effect = pythoncom.DoDragDrop(self._comData, source, shellcon.DROPEFFECT_COPY)
            logging.getLogger("IosFileTool").info("虚拟拖放返回 effect=%s async=%s dropped=%s", effect, self.inOperation, self.dropped)
            if not self.inOperation:
                self.finish(bool(effect & shellcon.DROPEFFECT_COPY) and not self.error)
        except pythoncom.com_error as ex:
            if ex.hresult != winerror.DRAGDROP_S_CANCEL:
                self.error = str(ex)
                logging.getLogger("IosFileTool").exception("虚拟拖放失败")
            self.finish(False)
        finally:
            self.inDrag = False
            uninitializeOle()

    # Esc、关窗或目标取消后，停止仅属于本次拖出的队列项
    def cancel(self):
        self.cancelled = True
        for itemId in self._tlId:
            self.queue.cancel(itemId)
        if not self.inDrag:
            self.finish(False)

    # 拖放或剪贴板接收端确认开始读取；剪贴板子类可以扩展操作生命周期
    def beginOperation(self):
        self.inOperation = True
        self.dropped = True

    # EndOperation 才是资源管理器完成写入的确认，不能把下载完成当作落点完成
    def finish(self, ok):
        if self.finished:
            return
        self.finished = True
        self.inOperation = False
        logging.getLogger("IosFileTool").info("虚拟拖放结束 success=%s downloaded=%d error=%s", ok, len(self._tlId), self.error)
        self.sigFinished.emit(self, bool(ok))
        self._comData = None
        self._data = None

    # 目录只枚举元数据，单个文件直接使用界面已有大小；这里绝不读内容
    def manifest(self):
        if self.tlTmManifest is None:
            if any(tm["isDir"] for tm in self.tlTmEntry):
                self.tlTmManifest = self._waitWorker(lambda: self.device.describeDrag(self.tlTmEntry))
            else:
                self.tlTmManifest = [dict(tm, relativePath=tm["name"]) for tm in self.tlTmEntry]
        return self.tlTmManifest

    # 临时目录和真正传输均推迟到落点首次读取内容；同一文件的重读共用结果
    def localFile(self, index):
        if self.cancelled or self.finished:
            raise COMException("拖放已结束或已中断", hresult=winerror.E_ABORT)
        if not self.dropped:
            raise COMException("尚未松手确认落点", hresult=winerror.E_PENDING)
        tm = self.manifest()[index]
        if tm["isDir"]:
            raise COMException(hresult=winerror.DV_E_LINDEX)
        if index not in self._tmLocal:
            if not self._folder:
                self._folder = tempfile.mkdtemp(prefix="IosFileToolVirtual-")
            # 每项独立子目录避免同名文件碰撞，给 Windows 的名称仍由描述符决定
            folder = os.path.join(self._folder, str(index))
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, tm["name"])
            tlId = self.queue.enqueuePull([{"name": tm["name"], "remotePath": tm["remotePath"], "localPath": path}], "拖出", self.device)
            self._tlId.extend(tlId)
            self._tmLocal[index] = (path, tlId)
            logging.getLogger("IosFileTool").info("落点开始读取，才提交手机传输 index=%d remote=%s", index, tm["remotePath"])
        path, tlId = self._tmLocal[index]
        self._waitQt(lambda: self.queue.allFinished(tlId))
        if self.cancelled or not self.queue.allDone(tlId):
            self.error = "手机传输失败或已中断，详见传输队列"
            raise COMException(self.error, hresult=winerror.E_ABORT)
        return path

    # COM 回调等待后台结果时继续处理 Qt 信号、进度与中断按钮
    def _waitQt(self, isDone):
        if isDone():
            return
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(20)
        # 只在后台真的结束后退出，确保失败不会被当成零字节文件
        def check():
            if isDone():
                loop.quit()
        timer.timeout.connect(check)
        timer.start()
        loop.exec()
        timer.stop()

    # 目录元数据在后台取，等待期间界面仍然响应；不传文件字节
    def _waitWorker(self, fn):
        done = threading.Event()
        tmResult = {}
        # 线程结果经事件交回当前 COM 所在线程，不在后台操作界面
        def run():
            try:
                tmResult["value"] = fn()
            except Exception as ex:
                tmResult["error"] = ex
                logging.getLogger("IosFileTool").exception("读取虚拟目录描述失败")
            finally:
                done.set()
        threading.Thread(target=run, name="ios-drag-metadata", daemon=True).start()
        self._waitQt(done.is_set)
        if "error" in tmResult:
            self.error = str(tmResult["error"])
            raise COMException(self.error, hresult=winerror.E_FAIL)
        return tmResult["value"]


# 用文件描述符而非伪造的本地路径对接资源管理器
class _FileDataObject:
    _com_interfaces_ = [pythoncom.IID_IDataObject, shell.IID_IAsyncOperation]
    _public_methods_ = ["GetData", "GetDataHere", "QueryGetData", "GetCanonicalFormatEtc", "SetData",
                        "EnumFormatEtc", "DAdvise", "DUnadvise", "EnumDAdvise", "SetAsyncMode",
                        "GetAsyncMode", "StartOperation", "InOperation", "EndOperation"]

    # 注册标准 Shell 格式；仅登记能力，不生成文件内容
    def __init__(self, session):
        self.session = session
        self.descriptor = win32clipboard.RegisterClipboardFormat("FileGroupDescriptorW")
        self.contents = win32clipboard.RegisterClipboardFormat("FileContents")
        self.phone = win32clipboard.RegisterClipboardFormat(IOS_FORMAT)
        self.effect = win32clipboard.RegisterClipboardFormat("Preferred DropEffect")
        self.dragLoop = win32clipboard.RegisterClipboardFormat("InShellDragLoop")
        self._tmExtra = {}

    # 格式枚举不需要枚举手机目录，也不能触发下载
    def EnumFormatEtc(self, direction):
        if direction != pythoncom.DATADIR_GET:
            raise COMException(hresult=winerror.E_NOTIMPL)
        tlFormat = [(cf, None, pythoncom.DVASPECT_CONTENT, -1, pythoncom.TYMED_HGLOBAL)
                    for cf in (self.descriptor, self.phone, self.effect, self.dragLoop)]
        tlFormat.append((self.contents, None, pythoncom.DVASPECT_CONTENT, -1, pythoncom.TYMED_ISTREAM))
        return NewEnum(tlFormat, iid=pythoncom.IID_IEnumFORMATETC)

    # 查询只检查格式；不得为了光标反馈读取手机文件
    def QueryGetData(self, fmt):
        cf, _target, aspect, index, medium = fmt
        if aspect != pythoncom.DVASPECT_CONTENT:
            raise COMException(hresult=winerror.DV_E_DVASPECT)
        if cf == self.contents:
            if not medium & pythoncom.TYMED_ISTREAM:
                raise COMException(hresult=winerror.DV_E_TYMED)
            if index < -1:
                raise COMException(hresult=winerror.DV_E_LINDEX)
        elif cf not in (self.descriptor, self.phone, self.effect, self.dragLoop) and cf not in self._tmExtra:
            raise COMException(hresult=winerror.DV_E_FORMATETC)
        elif not medium & pythoncom.TYMED_HGLOBAL:
            raise COMException(hresult=winerror.DV_E_TYMED)

    # 描述符仅含名字属性；内容返回惰性流，连 GetData 本身也不下载
    def GetData(self, fmt):
        self.QueryGetData(fmt)
        cf, _target, _aspect, index, _medium = fmt
        result = pythoncom.STGMEDIUM()
        if cf == self.contents:
            tlTm = self.session.manifest()
            if index < 0 or index >= len(tlTm) or tlTm[index]["isDir"]:
                raise COMException(hresult=winerror.DV_E_LINDEX)
            stream = wrap(_LazyFileStream(self.session, index), pythoncom.IID_IStream)
            result.set(pythoncom.TYMED_ISTREAM, stream)
            return result
        if cf == self.descriptor:
            payload = fileDescriptors(self.session.manifest())
        elif cf == self.phone:
            payload = json.dumps(self.session.tlTmEntry, ensure_ascii=False).encode("utf-8")
        elif cf == self.effect:
            payload = struct.pack("<I", shellcon.DROPEFFECT_COPY)
        elif cf == self.dragLoop:
            payload = struct.pack("<I", 0 if self.session.dropped else 1)
        else:
            payload = self._tmExtra[cf]
        result.set(pythoncom.TYMED_HGLOBAL, payload)
        return result

    # Shell 的拖动图像及落点反馈也可能附加自有二进制格式
    def SetData(self, fmt, medium, release=False):
        if medium.tymed == pythoncom.TYMED_HGLOBAL:
            self._tmExtra[fmt[0]] = medium.data

    # 此对象支持异步读取，Explorer 可以在松手后继续显示复制进度
    def GetAsyncMode(self):
        return self.session.asyncMode

    # 保留目标选择的异步模式
    def SetAsyncMode(self, enabled):
        self.session.asyncMode = bool(enabled)

    # 目标已接受拖放，开始异步提取；并不在这里抢先下载
    def StartOperation(self, _context):
        self.session.beginOperation()

    # DoDragDrop 返回后据此保持 App 容器锁定
    def InOperation(self):
        return self.session.inOperation

    # 资源管理器明确报告落点完成或失败
    def EndOperation(self, result, _context, effect):
        if result < 0 or not effect & shellcon.DROPEFFECT_COPY:
            self.session.cancel()
        self.session.finish(result >= 0 and bool(effect & shellcon.DROPEFFECT_COPY) and not self.session.error)

    # 此源不支持向调用方已有的介质写数据
    def GetDataHere(self, *args):
        raise COMException(hresult=winerror.E_NOTIMPL)

    # 返回相同格式，无需规范化转换
    def GetCanonicalFormatEtc(self, fmt):
        raise COMException(hresult=winerror.DATA_S_SAMEFORMATETC)

    # 文件内容在一次拖放中固定，不提供变更订阅
    def DAdvise(self, *args):
        raise COMException(hresult=winerror.OLE_E_ADVISENOTSUPPORTED)

    # 没有订阅可以取消
    def DUnadvise(self, *args):
        raise COMException(hresult=winerror.OLE_E_ADVISENOTSUPPORTED)

    # 没有订阅可以枚举
    def EnumDAdvise(self):
        raise COMException(hresult=winerror.OLE_E_ADVISENOTSUPPORTED)


# 读取流才会触发手机传输；Stat、Seek、Clone 都只处理元数据
class _LazyFileStream:
    _com_interfaces_ = [pythoncom.IID_IStream]
    _public_methods_ = ["Read", "Seek", "Stat", "Clone", "CopyTo", "Write", "SetSize", "Commit",
                        "Revert", "LockRegion", "UnlockRegion"]

    # 每个流及其克隆拥有独立读指针，共享已下载文件
    def __init__(self, session, index, position=0):
        self.session = session
        self.index = index
        self.position = position

    # Windows 松手后的实际内容读取，不把整份文件放进内存
    def Read(self, amount):
        if not amount:
            return b""
        path = self.session.localFile(self.index)
        try:
            with open(path, "rb") as f:
                f.seek(self.position)
                payload = f.read(amount)
            self.position += len(payload)
            return payload
        except OSError as ex:
            self.session.error = str(ex)
            logging.getLogger("IosFileTool").exception("向目标读取虚拟文件失败 index=%d", self.index)
            raise COMException(str(ex), hresult=winerror.E_FAIL) from ex

    # 流长度来自描述符，拖动期间不需要读文件
    def Seek(self, offset, origin):
        size = self.session.manifest()[self.index]["size"]
        position = offset if origin == 0 else self.position + offset if origin == 1 else size + offset
        if origin not in (0, 1, 2) or position < 0:
            raise COMException(hresult=winerror.STG_E_INVALIDFUNCTION)
        self.position = position
        return position

    # 标准 STATSTG，长度允许超过 4 GB
    def Stat(self, flags):
        tm = self.session.manifest()[self.index]
        return (None if flags & 1 else tm["name"], 2, tm["size"], FILE_TIME_ZERO, FILE_TIME_ZERO, FILE_TIME_ZERO,
                0, 0, pythoncom.IID_NULL, 0, 0)

    # 克隆只复制读指针，不下载也不共享指针位置
    def Clone(self):
        return wrap(_LazyFileStream(self.session, self.index, self.position), pythoncom.IID_IStream)

    # 限制复制缓冲区大小，大文件不整体装进内存
    def CopyTo(self, target, amount):
        done = 0
        while done < amount:
            payload = self.Read(min(amount - done, 1024 * 1024))
            if not payload:
                break
            written = target.Write(payload)
            done += len(payload)
            if written != len(payload):
                raise COMException(hresult=winerror.STG_E_MEDIUMFULL)
        return done, done

    # 手机源是只读的，拒绝目标写回
    def Write(self, *args):
        raise COMException(hresult=winerror.STG_E_ACCESSDENIED)

    # 不能从拖放接收端更改手机文件大小
    def SetSize(self, *args):
        raise COMException(hresult=winerror.STG_E_ACCESSDENIED)

    # 只读流没有需要提交的修改
    def Commit(self, *args):
        return None

    # 只读流没有事务可以回滚
    def Revert(self):
        raise COMException(hresult=winerror.STG_E_REVERTED)

    # 读取时不支持区域锁
    def LockRegion(self, *args):
        raise COMException(hresult=winerror.STG_E_INVALIDFUNCTION)

    # 没有区域锁可以解除
    def UnlockRegion(self, *args):
        raise COMException(hresult=winerror.STG_E_INVALIDFUNCTION)


# 标准 OLE 源决定松手、Esc 的语义；只在松手后放行内容读取
class _DropSource:
    _com_interfaces_ = [pythoncom.IID_IDropSource]
    _public_methods_ = ["QueryContinueDrag", "GiveFeedback"]

    # 整个鼠标手势只对应这一份固定选择
    def __init__(self, session):
        self.session = session

    # 取消没有传输，释放左键才进入目标的 Drop 回调
    def QueryContinueDrag(self, escapePressed, keyState):
        if escapePressed or self.session.cancelled:
            self.session.cancelled = True
            return winerror.DRAGDROP_S_CANCEL
        if not keyState & win32con.MK_LBUTTON:
            self.session.dropped = True
            return winerror.DRAGDROP_S_DROP
        return winerror.S_OK

    # 使用系统的允许、禁止、复制光标
    def GiveFeedback(self, _effect):
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        return winerror.DRAGDROP_S_USEDEFAULTCURSORS


# FILEGROUPDESCRIPTORW 带完整相对目录；空目录也有独立描述符
def fileDescriptors(tlTm):
    tlPart = [struct.pack("<I", len(tlTm))]
    for tm in tlTm:
        relative = tm["relativePath"].replace("/", "\\")
        name = relative.encode("utf-16le")
        if len(name) > 518 or "\0" in relative:
            raise COMException("Windows 虚拟文件路径过长或含无效字符：%s" % relative, hresult=winerror.E_INVALIDARG)
        flags = 0x80000000 | 0x4 | 0x4000
        size = 0 if tm["isDir"] else int(tm["size"])
        if not tm["isDir"]:
            flags |= 0x40
        tlPart.append(struct.pack("<I16s4iI3QII520s", flags, b"", 0, 0, 0, 0,
                                  0x10 if tm["isDir"] else 0x80, 0, 0, 0,
                                  size >> 32, size & 0xFFFFFFFF, name))
    return b"".join(tlPart)
