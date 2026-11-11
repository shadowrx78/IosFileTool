# -*- coding: utf-8 -*-

import asyncio
import os
import posixpath
import threading
import time
import tempfile
import logging
import uuid

from pymobiledevice3 import usbmux
from pymobiledevice3.exceptions import ConnectionTerminatedError, PyMobileDevice3Exception
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import MAXIMUM_READ_SIZE
from pymobiledevice3.services.house_arrest import HouseArrestService
from pymobiledevice3.services.installation_proxy import InstallationProxyService
from pymobiledevice3.services.springboard import SpringBoardServicesService


# 一条 USB 上的手机。协议调用都丢进常驻事件循环，避免和界面线程抢
class DeviceService:
    # 拉起后台事件循环
    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._runLoop, name="ios-lockdown", daemon=True)
        self._thread.start()
        self._lockdown = None
        self._house = None
        self._serial = ""
        self._connectionType = "USB"
        self._bundleId = ""
        self._uiHold = 0
        self._uiHoldLock = threading.Lock()
        self._uiBusy = threading.Event()
        self._transferBusy = threading.Event()
        self._scanCancel = None
        self.documentsOnly = False
        self.accessNote = ""
        self._progressHook = None
        self._cancel = False
        self._cancelEvent = None
        self._iconDir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cache", "icons")
        self._noIconPath = os.path.join(self._iconDir, "noicon.txt")
        self._browsePath = os.path.join(os.path.dirname(self._iconDir), "browse.txt")

    # 在协议线程里跑完一条协程。同一时间只跑一条，避免目录读取和传输抢同一个通道
    def _call(self, coro):
        async def _locked():
            async with self._opLock:
                return await coro
        self._ready.wait()
        with self._uiHoldLock:
            self._uiHold += 1
            self._uiBusy.set()
        try:
            fut = asyncio.run_coroutine_threadsafe(_locked(), self._loop)
            return fut.result()
        finally:
            with self._uiHoldLock:
                self._uiHold -= 1
                if self._uiHold <= 0:
                    self._uiHold = 0
                    self._uiBusy.clear()

    # 事件循环起来之后才能建锁
    def _runLoop(self):
        asyncio.set_event_loop(self._loop)
        self._opLock = asyncio.Lock()
        self._ready.set()
        self._loop.run_forever()

    # 列出 USB 和已开启无线同步的手机
    def listDevice(self):
        return self._call(self._listDevice())

    # 按序列号和连接方式连上手机
    def connect(self, serial, connectionType):
        if self._scanCancel is not None:
            self._scanCancel.set()
        return self._call(self._connect(serial, connectionType))

    # 已安装 App。用户 App 排在系统 App 前面
    def listApp(self):
        return self._call(self._listApp())

    # 给还没缓存图标的包逐个询问。就地改传入的列表
    def fetchIcons(self, tlTmApp):
        self._call(self._fillIcon(self._requireLockdown(), tlTmApp))
        return tlTmApp

    # 主窗口队列也可能使用独立的剪贴板来源，统一通知后台扫描为文件传输让路
    def setTransferBusy(self, busy):
        if busy:
            self._transferBusy.set()
        else:
            self._transferBusy.clear()

    # 三个独立连接共同领任务；切换设备后旧扫描立即失效
    def startBrowseScan(self, tlTmApp, onOne, onStatus=None):
        if self._scanCancel is not None:
            self._scanCancel.set()
        tlNeed = [tm["bundleId"] for tm in tlTmApp if not tm.get("browseKnown") or tm.get("browseStale")]
        if not tlNeed or not self._serial:
            if onStatus is not None:
                onStatus("访问检测：已有 %d 项结果" % len(tlTmApp))
            return
        self._scanCancel = threading.Event()
        threading.Thread(target=self._scanMain,
                         args=(tlNeed, onOne, self._serial, self._connectionType, self._scanCancel, onStatus),
                         name="ios-browse-scan", daemon=True).start()

    # 每次扫描携带固定设备和自己的取消标记，不能随主连接漂移
    def _scanMain(self, tlBundleId, onOne, serial, connectionType, cancelEvent, onStatus):
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._scanPools(tlBundleId, onOne, serial, connectionType, cancelEvent, onStatus))
        except Exception:
            logging.getLogger("IosFileTool").exception("后台可访问性扫描失败")
        finally:
            loop.close()

    # USB 与 Wi-Fi 都使用三路并发，小批量按数量缩减；workerCount 供同批实测对照
    async def _scanPools(self, tlBundleId, onOne, serial, connectionType, cancelEvent, onStatus, workerCount=3):
        queue = asyncio.Queue()
        for bundleId in tlBundleId:
            queue.put_nowait(bundleId)
        count = min(workerCount, len(tlBundleId))
        started = time.perf_counter()
        tmProgress = {"done": 0, "total": len(tlBundleId), "blocked": 0, "unknown": 0,
                      "failedWorker": 0, "tmPending": {}, "lastFlush": time.monotonic()}
        if onStatus is not None:
            onStatus("访问检测：0/%d（%d 路并行）" % (len(tlBundleId), count))
        try:
            await asyncio.gather(*(self._scanWorker(queue, onOne, serial, connectionType, cancelEvent, onStatus, tmProgress)
                                   for _ in range(count)))
        finally:
            self._flushBrowseResults(tmProgress)
        elapsed = time.perf_counter() - started
        logging.getLogger("IosFileTool").info(
            "访问扫描统计 link=%s workers=%d done=%d/%d blocked=%d unknown=%d failedWorkers=%d elapsed=%.3fs",
            connectionType, count, tmProgress["done"], tmProgress["total"], tmProgress["blocked"],
            tmProgress["unknown"], tmProgress["failedWorker"], elapsed)
        if onStatus is not None and not cancelEvent.is_set():
            if tmProgress["done"] < tmProgress["total"]:
                onStatus("访问检测暂停：已完成 %d/%d，连接不可用；已有标记保留，重新连接后可继续" % (
                    tmProgress["done"], tmProgress["total"]))
            else:
                onStatus("访问检测完成：%d 项，无法打开 %d，待确认 %d，用时 %.1f 秒" % (
                    tmProgress["done"], tmProgress["blocked"], tmProgress["unknown"], elapsed))

    # 批量合并缓存，避免每扫一项都整份读写文件；退出和取消时也保存已确认结果
    def _flushBrowseResults(self, tmProgress):
        tmPending = tmProgress["tmPending"]
        if not tmPending:
            return
        with _browseWriteLock:
            tmKnown = _readBrowse(self._browsePath)
            tmKnown.update(tmPending)
            _writeBrowse(self._browsePath, tmKnown)
        tmPending.clear()
        tmProgress["lastFlush"] = time.monotonic()

    # 单项异常至多重连重试一次，不能因首个 App 断开服务就丢掉后面的整批
    async def _scanWorker(self, queue, onOne, serial, connectionType, cancelEvent, onStatus, tmProgress):
        if cancelEvent.is_set():
            return
        lockdown = None
        bundleId = None
        try:
            lockdown = await create_using_usbmux(serial=serial, connection_type=connectionType, autopair=False)
            while not cancelEvent.is_set():
                while (self._uiBusy.is_set() or self._transferBusy.is_set()) and not cancelEvent.is_set():
                    await asyncio.sleep(0.1)
                if cancelEvent.is_set():
                    return
                try:
                    bundleId = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                ok = await _canBrowseOn(lockdown, bundleId)
                if ok is None and not cancelEvent.is_set():
                    # HouseArrest 的单条服务断开不能证明手机整体失联
                    try:
                        await lockdown.close()
                    except Exception:
                        pass
                    lockdown = await create_using_usbmux(serial=serial, connection_type=connectionType, autopair=False)
                    ok = await _canBrowseOn(lockdown, bundleId)
                if cancelEvent.is_set():
                    return
                if ok is not None:
                    tmProgress["tmPending"][bundleId] = ok
                    if (len(tmProgress["tmPending"]) >= 10
                            or time.monotonic() - tmProgress["lastFlush"] >= 1):
                        self._flushBrowseResults(tmProgress)
                tmProgress["done"] += 1
                tmProgress["blocked"] += int(ok is False)
                tmProgress["unknown"] += int(ok is None)
                if onOne is not None:
                    onOne(bundleId, ok)
                if onStatus is not None:
                    onStatus("访问检测：%d/%d，无法打开 %d，待确认 %d" % (
                        tmProgress["done"], tmProgress["total"], tmProgress["blocked"], tmProgress["unknown"]))
                bundleId = None
        except Exception:
            logging.getLogger("IosFileTool").exception("单路访问扫描退出，交由其它连接继续 link=%s", connectionType)
            tmProgress["failedWorker"] += 1
            if bundleId is not None and not cancelEvent.is_set():
                queue.put_nowait(bundleId)
        finally:
            if lockdown is not None:
                try:
                    await lockdown.close()
                except Exception:
                    logging.getLogger("IosFileTool").exception("关闭扫描连接失败")

    # 打开某个 App 的容器。整容器被拒绝时退回 Documents
    def openApp(self, bundleId):
        return self._call(self._openApp(bundleId))

    # 列当前 App 某个目录
    def listDir(self, remoteDir):
        return self._call(self._listDir(remoteDir))

    # 在当前目录下新建空文件夹。同名时不覆盖
    def makeDir(self, remoteDir, name):
        return self._call(self._makeDir(remoteDir, name))

    # 名称只作为这一层的一段，不能带路径
    async def _makeDir(self, remoteDir, name):
        name = (name or "").strip()
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            raise RuntimeError("文件夹名称不能为空，也不能包含斜杠")
        house = self._requireHouse()
        remotePath = posixpath.join(remoteDir or "/", name)
        if await house.exists(remotePath):
            raise RuntimeError("已经有同名文件或文件夹：%s" % name)
        await house.makedirs(remotePath)

    # 虚拟目录描述只取名称、类型和长度，不读取文件内容
    def describeDrag(self, tlTm):
        return self._call(self._describeDrag(tlTm))

    # 按父目录在前的顺序列出完整相对目录，空目录也保留
    async def _describeDrag(self, tlTm):
        house = self._requireHouse()
        tlResult = []
        # 每个路径只做 stat 和 listdir，不进行 fopen / fread
        async def visit(remotePath, relativePath):
            info = await house.stat(remotePath)
            isDir = info.get("st_ifmt") == "S_IFDIR"
            tlResult.append({"name": posixpath.basename(remotePath), "remotePath": remotePath,
                             "relativePath": relativePath, "isDir": isDir,
                             "size": 0 if isDir else int(info.get("st_size") or 0)})
            if isDir:
                for name in await house.listdir(remotePath):
                    if name not in (".", ".."):
                        await visit(posixpath.join(remotePath, name), relativePath + "/" + name)
        for tm in _dropNestedRemote(tlTm):
            await visit(tm["remotePath"], tm["name"])
        return tlResult

    # 让当前正在跑的那一条传输在下一块数据时停下来
    def requestCancel(self):
        self._cancel = True

    # 下一条传输开始前清掉上次的中断
    def clearCancel(self):
        self._cancel = False

    # 每条队列项携带独立中断标记，排队等待协议锁期间的中断也不能丢失
    def pullOne(self, remotePath, localPath, onProgress, cancelEvent=None):
        self._call(self._pullOne(remotePath, localPath, onProgress, cancelEvent))

    # 导入同样把中断标记带进协议线程
    def pushOne(self, localPath, remoteDir, onProgress, cancelEvent=None):
        self._call(self._pushOne(localPath, remoteDir, onProgress, cancelEvent))

    # 剪贴板保存固定的来源身份，后续切换浏览目录不会改变来源
    def context(self):
        return {"serial": self._serial, "connectionType": self._connectionType, "bundleId": self._bundleId}

    # 手机内复制在同一 AFC 会话里按块读取、写入，不占用本地完整文件缓存
    def copyOne(self, remotePath, remoteDir, onProgress, cancelEvent=None):
        return self._call(self._copyOne(remotePath, remoteDir, onProgress, cancelEvent))

    # 先复制到独立暂存项，成功后再发布为目标名；中断只清理本次暂存项
    async def _copyOne(self, remotePath, remoteDir, onProgress, cancelEvent=None):
        self._cancelEvent = cancelEvent
        self.clearCancel()
        self._checkCancel()
        house = self._requireHouse()
        source = posixpath.normpath(remotePath)
        destDir = posixpath.normpath(remoteDir)
        if source == "/" or destDir == source or destDir.startswith(source.rstrip("/") + "/"):
            raise RuntimeError("不能把文件夹复制到自己或自己的子目录")
        target = await self._copyTarget(house, source, destDir)
        staging = posixpath.join(destDir, ".IosFileTool-copy-" + uuid.uuid4().hex)
        total = await self._remoteBytes(house, source)
        counter = _ByteProgress(total, onProgress, self._checkCancel)
        try:
            await self._copyPath(house, source, staging, counter)
            self._checkCancel()
            if await house.exists(target):
                raise FileExistsError("复制期间目标名称已被占用，未覆盖：%s" % target)
            await house.rename(staging, target)
            counter.finish()
            return target
        except Exception:
            try:
                if await house.exists(staging):
                    await house.rm(staging)
            except Exception:
                logging.getLogger("IosFileTool").exception("清理本次复制暂存项失败：%s", staging)
            raise

    # 有重名时生成“名称 - 副本”及递增序号，文件夹名不拆扩展名
    async def _copyTarget(self, house, source, destDir):
        name = posixpath.basename(source)
        target = posixpath.join(destDir, name)
        if not await house.exists(target):
            return target
        info = await house.stat(source)
        stem, ext = (name, "") if info.get("st_ifmt") == "S_IFDIR" else posixpath.splitext(name)
        number = 1
        while True:
            suffix = " - 副本" if number == 1 else " - 副本 (%d)" % number
            target = posixpath.join(destDir, stem + suffix + ext)
            if not await house.exists(target):
                return target
            number += 1

    # 目录递归复制保留空目录，文件按固定块大小传递
    async def _copyPath(self, house, source, target, counter):
        self._checkCancel()
        info = await house.stat(source)
        if info.get("st_ifmt") == "S_IFDIR":
            tlName = await house.listdir(source)
            await house.makedirs(target)
            for name in tlName:
                if name and name not in (".", ".."):
                    await self._copyPath(house, posixpath.join(source, name), posixpath.join(target, name), counter)
            return
        sourceHandle = await house.fopen(source)
        targetHandle = None
        try:
            targetHandle = await house.fopen(target, "w")
            left = int(info.get("st_size") or 0)
            while left:
                self._checkCancel()
                chunk = await house.fread(sourceHandle, min(MAXIMUM_READ_SIZE, left))
                if not chunk:
                    raise IOError("复制时源文件提前结束：%s" % source)
                await house.fwrite(targetHandle, chunk)
                left -= len(chunk)
                counter.add(len(chunk))
        finally:
            try:
                if targetHandle is not None:
                    await house.fclose(targetHandle)
            finally:
                await house.fclose(sourceHandle)

    # 同一 App 内使用 AFC 原生移动；目标已存在时拒绝覆盖
    def moveOne(self, remotePath, remoteDir, renameOnConflict=False):
        return self._call(self._moveOne(remotePath, remoteDir, renameOnConflict))

    # 移动前校验自身、子目录和同名冲突，避免覆盖已有手机文件
    async def _moveOne(self, remotePath, remoteDir, renameOnConflict=False):
        house = self._requireHouse()
        source = posixpath.normpath(remotePath)
        destDir = posixpath.normpath(remoteDir)
        if source == "/" or destDir == source or destDir.startswith(source.rstrip("/") + "/"):
            raise RuntimeError("不能把文件夹移动到自己或自己的子目录")
        target = posixpath.join(destDir, posixpath.basename(source))
        if target == source:
            return
        if await house.exists(target):
            if not renameOnConflict:
                raise FileExistsError("目标已存在，未移动：%s" % target)
            target = await self._copyTarget(house, source, destDir)
        await house.rename(source, target)
        return target

    # 把选中的手机路径导出到同一个本地目录
    def pullMany(self, tlTm, localDir):
        self._call(self._pullMany(tlTm, localDir))

    # 把多个本地路径推进当前手机目录
    def pushMany(self, tlPath, remoteDir):
        self._call(self._pushMany(tlPath, remoteDir))

    # 删掉手机上的一个文件或整个目录。父目录已选中时不再删子项
    def removeMany(self, tlTm):
        self._call(self._removeMany(tlTm))

    # 断开锁屏和文件通道
    def close(self):
        if self._scanCancel is not None:
            self._scanCancel.set()
        self._call(self._closeAll())

    # 专用剪贴板来源不再使用时，连同其事件循环线程一起释放
    def dispose(self):
        try:
            self.close()
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(3)
            if not self._thread.is_alive():
                self._loop.close()

    # 传输和读图标时回传一句进度，由界面线程去画
    def setProgressHook(self, fn):
        self._progressHook = fn

    # USB 排前面。同一台同时插着线和开着无线时，两条都留着
    async def _listDevice(self):
        tlDev = await usbmux.list_devices()
        tlTm = []
        tlsSeen = set()
        for dev in tlDev:
            connectionType = getattr(dev, "connection_type", "") or ""
            if connectionType not in ("USB", "Network"):
                continue
            serial = getattr(dev, "serial", "") or ""
            if not serial:
                continue
            key = serial + "\0" + connectionType
            if key in tlsSeen:
                continue
            tlsSeen.add(key)
            tlTm.append({"serial": serial, "connectionType": connectionType})
        tlTm.sort(key=lambda tm: (0 if tm["connectionType"] == "USB" else 1, tm["serial"]))
        return tlTm

    # 无线走的是同一套锁屏服务，前提是这台电脑曾经配对过
    async def _connect(self, serial, connectionType):
        await self._closeAll()
        self._connectionType = connectionType
        try:
            lockdown = await create_using_usbmux(
                serial=serial,
                connection_type=connectionType,
                autopair=connectionType == "USB",
                pair_timeout=120,
            )
        except PyMobileDevice3Exception as ex:
            raise RuntimeError(self._explain(ex)) from ex
        self._lockdown = lockdown
        self._serial = serial
        self._connectionType = connectionType
        name = _deviceName(lockdown) or serial
        version = lockdown.product_version or ""
        link = "USB" if connectionType == "USB" else "Wi-Fi"
        return {"name": name, "version": version, "serial": serial, "link": link}

    # 安装代理一次拉全表，界面再筛系统 App
    async def _listApp(self):
        lockdown = self._requireLockdown()
        raw = await InstallationProxyService(lockdown=lockdown).get_apps(application_type="Any")
        tlTmApp = []
        for bundleId, info in raw.items():
            if not isinstance(info, dict):
                continue
            appType = info.get("ApplicationType") or ""
            name = info.get("CFBundleDisplayName") or info.get("CFBundleName") or bundleId
            tlTmApp.append({
                "bundleId": bundleId,
                "name": name,
                "appType": appType,
                "hasIcon": False,
                "iconPath": "",
                "iconKnown": False,
                "canBrowse": True,
                "browseKnown": False,
            })
        self._applyIconCache(tlTmApp)
        self._applyBrowseCache(tlTmApp)
        tlTmApp.sort(key=lambda tm: (
            0 if tm["hasIcon"] else 1,
            0 if tm["appType"] == "User" else 1,
            tm["name"].lower(),
            tm["bundleId"],
        ))
        return tlTmApp

    # 读上次检测结果
    def _applyBrowseCache(self, tlTmApp):
        tmKnown = _readBrowse(self._browsePath)
        for tm in tlTmApp:
            if tm["bundleId"] in tmKnown:
                tm["canBrowse"] = tmKnown[tm["bundleId"]]
                tm["browseKnown"] = True
                # 先显示已有红标，再在后台复查旧的负面结果
                tm["browseStale"] = not tm["canBrowse"]

    # 逐个试开。检测完若之前开着某个 App，再打开回去
    async def _probeBrowse(self, tlTmApp):
        self._applyBrowseCache(tlTmApp)
        tlNeed = []
        for tm in tlTmApp:
            if not tm["browseKnown"]:
                tlNeed.append(tm)
        saved = self._bundleId
        tmKnown = _readBrowse(self._browsePath)
        try:
            total = len(tlNeed)
            for index, tm in enumerate(tlNeed):
                self._report("检测能否浏览 %d/%d  %s" % (index + 1, total, tm["name"]))
                ok = await self._canBrowse(tm["bundleId"])
                tm["canBrowse"] = ok
                tm["browseKnown"] = True
                tmKnown[tm["bundleId"]] = ok
        finally:
            _writeBrowse(self._browsePath, tmKnown)
            if saved:
                try:
                    await self._openApp(saved)
                except Exception:
                    pass
            elif self._house is not None:
                await self._house.close()
                self._house = None
                self._bundleId = ""

    # 能列出至少一层就算可浏览，两种通道都失败则不可浏览
    async def _canBrowse(self, bundleId):
        try:
            await self._openApp(bundleId)
            await self._listDir("/")
        except Exception:
            return False
        return True

    # 只读本机缓存，列表可以先出来
    def _applyIconCache(self, tlTmApp):
        os.makedirs(self._iconDir, exist_ok=True)
        tlsNoIcon = _readNoIcon(self._noIconPath)
        for tm in tlTmApp:
            bundleId = tm["bundleId"]
            iconPath = _iconFile(self._iconDir, bundleId)
            if os.path.isfile(iconPath) and os.path.getsize(iconPath) > 0:
                tm["hasIcon"] = True
                tm["iconPath"] = iconPath
                tm["iconKnown"] = True
            elif bundleId in tlsNoIcon:
                tm["hasIcon"] = False
                tm["iconKnown"] = True

    # 有主屏幕图标的视为应用，取不到的视为内部包。结果缓存在本机
    async def _fillIcon(self, lockdown, tlTmApp):
        os.makedirs(self._iconDir, exist_ok=True)
        tlsNoIcon = _readNoIcon(self._noIconPath)
        tlNeed = []
        for tm in tlTmApp:
            if tm.get("iconKnown"):
                continue
            bundleId = tm["bundleId"]
            iconPath = _iconFile(self._iconDir, bundleId)
            if os.path.isfile(iconPath) and os.path.getsize(iconPath) > 0:
                tm["hasIcon"] = True
                tm["iconPath"] = iconPath
                tm["iconKnown"] = True
                continue
            if bundleId in tlsNoIcon:
                tm["hasIcon"] = False
                tm["iconKnown"] = True
                continue
            tlNeed.append(tm)
        if not tlNeed:
            return
        springboard = SpringBoardServicesService(lockdown=lockdown)
        try:
            total = len(tlNeed)
            for index, tm in enumerate(tlNeed):
                self._report("读取图标 %d/%d  %s" % (index + 1, total, tm["name"]))
                png = await self._loadIconPng(springboard, tm["bundleId"])
                iconPath = _iconFile(self._iconDir, tm["bundleId"])
                if png:
                    with open(iconPath, "wb") as f:
                        f.write(png)
                    tm["hasIcon"] = True
                    tm["iconPath"] = iconPath
                    tm["iconKnown"] = True
                else:
                    tlsNoIcon.add(tm["bundleId"])
                    tm["hasIcon"] = False
                    tm["iconKnown"] = True
        finally:
            await springboard.close()
            _writeNoIcon(self._noIconPath, tlsNoIcon)

    # 单个包没有图标时不要打断整次列表
    async def _loadIconPng(self, springboard, bundleId):
        try:
            png = await springboard.get_icon_pngdata(bundleId)
        except Exception:
            return b""
        if not png:
            return b""
        return png

    # VendContainer 失败再 VendDocuments，两次都失败就把原因抛回去
    async def _openApp(self, bundleId):
        lockdown = self._requireLockdown()
        if self._house is not None:
            await self._house.close()
            self._house = None
            self._bundleId = ""
        house = None
        documentsOnly = False
        errContainer = None
        try:
            house = await HouseArrestService.create(lockdown, bundleId, documents_only=False)
        except Exception as ex:
            errContainer = ex
        if house is None:
            try:
                house = await HouseArrestService.create(lockdown, bundleId, documents_only=True)
                documentsOnly = True
            except Exception as ex:
                raise RuntimeError(self._explainOpen(bundleId, errContainer, ex)) from ex
        self._house = house
        self._bundleId = bundleId
        self.documentsOnly = documentsOnly
        self.accessNote = "仅 Documents" if documentsOnly else "整个容器"
        return {"bundleId": bundleId, "documentsOnly": documentsOnly}

    # 根目录经常被拒绝，这时改探 Documents / Library 等已知子目录
    async def _listDir(self, remoteDir):
        house = self._requireHouse()
        remoteDir = remoteDir or "/"
        if remoteDir == "/":
            return await self._listRoot(house)
        try:
            return await self._entriesIn(house, remoteDir)
        except Exception as ex:
            if not _isPermDenied(ex):
                raise
            raise RuntimeError("这个目录没有读取权限：%s" % remoteDir) from ex

    # / 列不出来时，把能 stat 到的顶层目录当成根
    async def _listRoot(self, house):
        try:
            tlTm = await self._entriesIn(house, "/")
            self.accessNote = "仅 Documents" if self.documentsOnly else "整个容器"
            return tlTm
        except Exception as ex:
            if not _isPermDenied(ex):
                raise
        tlTm = await self._probeTop(house)
        if tlTm:
            self.accessNote = "部分目录"
            return tlTm
        house = await self._reopenDocuments()
        try:
            await house.stat("/Documents")
        except Exception as ex:
            raise RuntimeError("这个 App 没有对电脑开放文件目录。%s" % ex) from ex
        self.accessNote = "仅 Documents"
        return [_dirEntry("Documents", "/Documents")]

    # 整容器会话里逐个试常见顶层目录
    async def _probeTop(self, house):
        tlTm = []
        for name in ("Documents", "Library", "tmp", "StoreKit"):
            remotePath = "/" + name
            try:
                info = await house.stat(remotePath)
            except Exception:
                continue
            if info.get("st_ifmt") != "S_IFDIR":
                continue
            tm = _dirEntry(name, remotePath)
            try:
                await house.listdir(remotePath)
            except Exception:
                tm["blocked"] = True
            tlTm.append(tm)
        return tlTm

    # 当前通道读不了任何顶层目录时，改用只开放 Documents 的通道
    async def _reopenDocuments(self):
        bundleId = self._bundleId
        lockdown = self._requireLockdown()
        if self._house is not None:
            await self._house.close()
            self._house = None
        house = await HouseArrestService.create(lockdown, bundleId, documents_only=True)
        self._house = house
        self.documentsOnly = True
        return house

    # 列出一个目录里的文件和子目录
    async def _entriesIn(self, house, remoteDir):
        names = await house.listdir(remoteDir)
        tlTmEntry = []
        for name in names:
            if not name or name in (".", ".."):
                continue
            remotePath = posixpath.join(remoteDir, name)
            info = await house.stat(remotePath)
            isDir = info.get("st_ifmt") == "S_IFDIR"
            tlTmEntry.append({
                "name": name,
                "remotePath": remotePath,
                "isDir": isDir,
                "size": 0 if isDir else int(info.get("st_size") or 0),
                "blocked": False,
            })
        for tm in tlTmEntry:
            if not tm["isDir"]:
                continue
            try:
                await house.listdir(tm["remotePath"])
            except Exception:
                tm["blocked"] = True
        tlTmEntry.sort(key=lambda tm: (1 if tm["blocked"] else 0, 0 if tm["isDir"] else 1, tm["name"].lower()))
        return tlTmEntry

    # 下一块之前看有没有人按下中断
    def _checkCancel(self):
        if self._cancel or (self._cancelEvent is not None and self._cancelEvent.is_set()):
            raise Cancelled()

    # 导出一条，大小先算好，方便这一项自己的百分比
    async def _pullOne(self, remotePath, localPath, onProgress, cancelEvent=None):
        self._cancelEvent = cancelEvent
        self.clearCancel()
        self._checkCancel()
        house = self._requireHouse()
        total = await self._remoteBytes(house, remotePath)
        counter = _ByteProgress(total, onProgress, self._checkCancel)
        await self._pullPath(house, remotePath, localPath, counter)
        counter.finish()

    # 导入一条。中断时尽量删掉已经写到手机上的半截
    async def _pushOne(self, localPath, remoteDir, onProgress, cancelEvent=None):
        self._cancelEvent = cancelEvent
        self.clearCancel()
        self._checkCancel()
        house = self._requireHouse()
        total = _localBytes(localPath)
        counter = _ByteProgress(total, onProgress, self._checkCancel)
        remotePath = posixpath.join(remoteDir, os.path.basename(localPath))
        existed = await house.exists(remotePath)
        try:
            await self._pushPath(house, localPath, remoteDir, counter)
            counter.finish()
        except Cancelled:
            if existed:
                logging.getLogger("IosFileTool").warning("导入中断，保留原有目录及已写入内容：%s", remotePath)
            try:
                if not existed and await house.exists(remotePath):
                    await house.rm(remotePath)
            except Exception:
                pass
            raise

    # 进度条关掉，避免往控制台刷。字节进度由自己的拷贝循环报
    async def _pullToDir(self, remotePath, localDir):
        await self._pullMany([{"name": posixpath.basename(remotePath), "remotePath": remotePath, "isDir": False}], localDir)

    # 多个手机路径按总字节一起算进度。被别的选中项包住的路径跳过，避免传两遍
    async def _pullMany(self, tlTm, localDir):
        house = self._requireHouse()
        tlTm = _dropNestedRemote(tlTm)
        total = 0
        for tm in tlTm:
            total += await self._remoteBytes(house, tm["remotePath"])
        counter = _TransferCounter(total, "导出", len(tlTm), self._report)
        for index, tm in enumerate(tlTm):
            counter.beginItem(index, tm["name"])
            localPath = os.path.join(localDir, tm["name"])
            await self._pullPath(house, tm["remotePath"], localPath, counter)
        counter.finish()

    # 远端路径是目录时，文件放进这个目录
    async def _pushInto(self, localPath, remoteDir):
        await self._pushMany([localPath], remoteDir)

    # 多个本地路径共用一个进度。子路径被父目录包住时只传父目录
    async def _pushMany(self, tlPath, remoteDir):
        house = self._requireHouse()
        tlPath = _dropNestedLocal(tlPath)
        total = 0
        for localPath in tlPath:
            total += _localBytes(localPath)
        counter = _TransferCounter(total, "导入", len(tlPath), self._report)
        for index, localPath in enumerate(tlPath):
            counter.beginItem(index, os.path.basename(localPath))
            await self._pushPath(house, localPath, remoteDir, counter)
        counter.finish()

    # 统计要导出的字节，好算总百分比
    async def _remoteBytes(self, house, remotePath):
        self._checkCancel()
        info = await house.stat(remotePath)
        if info.get("st_ifmt") != "S_IFDIR":
            return int(info.get("st_size") or 0)
        total = 0
        for name in await house.listdir(remotePath):
            if not name or name in (".", ".."):
                continue
            total += await self._remoteBytes(house, posixpath.join(remotePath, name))
        return total

    # 把手机上的文件或目录树拷到本地路径
    async def _pullPath(self, house, remotePath, localPath, counter):
        self._checkCancel()
        info = await house.stat(remotePath)
        if info.get("st_ifmt") == "S_IFDIR":
            os.makedirs(localPath, exist_ok=True)
            for name in await house.listdir(remotePath):
                if not name or name in (".", ".."):
                    continue
                await self._pullPath(house, posixpath.join(remotePath, name), os.path.join(localPath, name), counter)
            return
        await self._pullFile(house, remotePath, localPath, int(info.get("st_size") or 0), counter)

    # 先写同目录临时文件，完整读完才替换目标，中断不会删除或截断原文件
    async def _pullFile(self, house, remotePath, localPath, size, counter):
        parent = os.path.dirname(localPath) or "."
        os.makedirs(parent, exist_ok=True)
        handle = await house.fopen(remotePath)
        partPath = ""
        try:
            with tempfile.NamedTemporaryFile(mode="wb", prefix=".ios-", suffix=".part", dir=parent, delete=False) as f:
                partPath = f.name
                left = size
                while left > 0:
                    self._checkCancel()
                    chunk = await house.fread(handle, min(MAXIMUM_READ_SIZE, left))
                    if not chunk:
                        raise IOError("手机读取提前结束：%s，尚缺 %d 字节" % (remotePath, left))
                    f.write(chunk)
                    left -= len(chunk)
                    counter.add(len(chunk))
            self._checkCancel()
            os.replace(partPath, localPath)
            partPath = ""
        finally:
            try:
                await house.fclose(handle)
            finally:
                if partPath and os.path.isfile(partPath):
                    os.remove(partPath)
        if size == 0:
            counter.add(0)

    # 把本地文件或目录推进远端目录
    async def _pushPath(self, house, localPath, remoteDir, counter):
        self._checkCancel()
        name = os.path.basename(localPath)
        remotePath = posixpath.join(remoteDir, name)
        if os.path.isdir(localPath):
            if not await house.exists(remotePath):
                await house.makedirs(remotePath)
            for child in os.listdir(localPath):
                await self._pushPath(house, os.path.join(localPath, child), remotePath, counter)
            return
        await self._pushFile(house, localPath, remotePath, counter)

    # 按块写一个文件
    async def _pushFile(self, house, localPath, remotePath, counter):
        handle = await house.fopen(remotePath, "w")
        try:
            with open(localPath, "rb") as f:
                while True:
                    self._checkCancel()
                    chunk = f.read(MAXIMUM_READ_SIZE)
                    if not chunk:
                        break
                    await house.fwrite(handle, chunk)
                    counter.add(len(chunk))
        finally:
            await house.fclose(handle)

    # 界面线程可以安全收到的一句进度
    def _report(self, text):
        fn = self._progressHook
        if fn is not None:
            fn(text)

    # 目录会连同里面的内容一起删。先删子路径会在父目录已经删掉后失败
    async def _removeMany(self, tlTm):
        house = self._requireHouse()
        tlTm = _dropNestedRemote(tlTm)
        total = len(tlTm)
        for index, tm in enumerate(tlTm):
            self._report("删除 %d/%d  %s" % (index + 1, total, tm["name"]))
            await house.rm(tm["remotePath"])

    # 先关文件通道再关锁屏
    async def _closeAll(self):
        if self._house is not None:
            await self._house.close()
            self._house = None
            self._bundleId = ""
        if self._lockdown is not None:
            await self._lockdown.close()
            self._lockdown = None
            self._serial = ""

    # 还没连上就点了文件操作
    def _requireLockdown(self):
        if self._lockdown is None:
            raise RuntimeError("还没有连接手机")
        return self._lockdown

    # 还没进 App 就列目录
    def _requireHouse(self):
        if self._house is None:
            raise RuntimeError("还没有打开 App")
        return self._house

    # 把驱动和信任弹窗翻成能看懂的句子
    def _explain(self, ex):
        text = str(ex)
        low = text.lower()
        if self._connectionType == "Network" and _isConnectionError(ex):
            return "Wi-Fi 连接已断开。请唤醒手机，确认与电脑仍在同一网络，再刷新设备并重新连接。详细错误已写入日志。"
        if "usbmux" in low or "apple mobile device" in low or "winerror" in low:
            return "连不上 Apple 的 USB 服务。请先安装「Apple 设备」或 iTunes，再用数据线连接并在手机上点信任。\n%s" % text
        if "pair" in low or "trust" in low or "password" in low:
            return "手机还没信任这台电脑，或正在等锁屏密码。解开手机并点信任后再试。\n%s" % text
        return text

    # 整容器和 Documents 都拒绝时的说明
    def _explainOpen(self, bundleId, errContainer, errDocuments):
        if self._connectionType == "Network" and (_isConnectionError(errContainer) or _isConnectionError(errDocuments)):
            return self._explain(errDocuments if _isConnectionError(errDocuments) else errContainer)
        return "打不开 %s。整容器：%s；Documents：%s" % (bundleId, errContainer, errDocuments)


# 图标文件名沿用 Bundle ID，斜杠换成下划线
def _iconFile(iconDir, bundleId):
    safe = bundleId.replace("/", "_").replace("\\", "_")
    return os.path.normpath(os.path.join(iconDir, safe + ".png"))


# 已经确认没有图标的包，下次不再问手机
def _readNoIcon(path):
    tls = set()
    if not os.path.isfile(path):
        return tls
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            bundleId = line.strip()
            if bundleId:
                tls.add(bundleId)
    return tls


# 把没有图标的包名写回缓存
def _writeNoIcon(path, tlsNoIcon):
    with open(path, "w", encoding="utf-8") as f:
        for bundleId in sorted(tlsNoIcon):
            f.write(bundleId + "\n")


# 选中项里若同时有目录和它下面的路径，只留目录
def _dropNestedRemote(tlTm):
    tlKeep = []
    for tm in tlTm:
        path = tm["remotePath"]
        nested = False
        for other in tlTm:
            parent = other["remotePath"].rstrip("/")
            if parent and path != other["remotePath"] and path.startswith(parent + "/"):
                nested = True
                break
        if not nested:
            tlKeep.append(tm)
    return tlKeep


# 本地路径同样去掉被父目录包含的项
def _dropNestedLocal(tlPath):
    tlNorm = []
    for path in tlPath:
        tlNorm.append(os.path.normcase(os.path.abspath(path)))
    tlKeep = []
    for index, path in enumerate(tlPath):
        norm = tlNorm[index]
        nested = False
        for otherIndex, other in enumerate(tlNorm):
            if otherIndex == index:
                continue
            prefix = other.rstrip("\\/")
            if prefix and norm.startswith(prefix + os.sep):
                nested = True
                break
        if not nested:
            tlKeep.append(path)
    return tlKeep


# 本地文件或目录的总字节
def _localBytes(path):
    if not os.path.isdir(path):
        return os.path.getsize(path)
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            total += os.path.getsize(os.path.join(root, name))
    return total


# 用户中断当前这一条传输
class Cancelled(Exception):
    pass


# 单条传输的字节计数
class _ByteProgress:
    # onProgress(done, total, speed) 由调用方更新自己的那一行
    def __init__(self, total, onProgress, checkCancel):
        self.total = total
        self.done = 0
        self._onProgress = onProgress
        self._checkCancel = checkCancel
        self._t0 = time.perf_counter()
        self._last = 0

    # 累加一块，并限制刷新频率
    def add(self, n):
        self._checkCancel()
        self.done += n
        now = time.perf_counter()
        if self.done < self.total and now - self._last < 0.15:
            return
        self._emit()

    # 这一条结束
    def finish(self):
        self.done = max(self.done, self.total)
        self._emit()

    # 算出速度再交给界面
    def _emit(self):
        self._last = time.perf_counter()
        speed = self.done / max(self._last - self._t0, 0.001)
        if self._onProgress is not None:
            self._onProgress(self.done, self.total, speed)


# 一批传输共用的字节计数，用来算百分比和速度
class _TransferCounter:
    # total 是这一批的总字节，count 是路径条数
    def __init__(self, total, action, count, report):
        self.total = total
        self.done = 0
        self.action = action
        self.count = count
        self._report = report
        self._t0 = time.perf_counter()
        self._last = 0
        self._index = 0
        self._name = ""

    # 换到下一条路径时立刻刷新文字
    def beginItem(self, index, name):
        self._index = index
        self._name = name
        self._emit()

    # 累加刚传完的字节，大约每 0.15 秒刷新一次
    def add(self, n):
        self.done += n
        now = time.perf_counter()
        if self.done < self.total and now - self._last < 0.15:
            return
        self._emit()

    # 结束时把百分比推到 100
    def finish(self):
        self.done = max(self.done, self.total)
        self._emit()

    # 拼成界面上的一行
    def _emit(self):
        self._last = time.perf_counter()
        elapsed = max(self._last - self._t0, 0.001)
        percent = 100 if self.total <= 0 else int(self.done * 100 / self.total)
        speed = self.done / elapsed
        self._report("%s %d/%d  %s  %d%%  %s/s" % (
            self.action,
            self._index + 1,
            self.count,
            self._name,
            percent,
            _formatSpeed(speed),
        ))


# 每秒字节数
def _formatSpeed(speed):
    n = float(speed)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            if unit == "B":
                return "%d B" % int(n)
            return "%.1f %s" % (n, unit)
        n /= 1024
    return "%d B" % int(speed)


# 锁屏里的设备名。没有就退回空，调用方再用序列号
def _deviceName(lockdown):
    name = lockdown.display_name or ""
    if name:
        return name
    values = getattr(lockdown, "all_values", None) or {}
    return values.get("DeviceName") or ""


# 明确的连接错误不能当作 App 无权限，也不能缓存成永久禁用
def _isConnectionError(ex):
    text = str(ex).lower()
    return isinstance(ex, (ConnectionError, TimeoutError, OSError, EOFError)) or any(
        word in text for word in ("device not found", "no device", "usbmux", "connection", "timed out",
                                  "timeout", "broken pipe", "eof", "winerror", "passwordprotected"))


# 返回 True / False / None：可访问、明确拒绝、通讯失败或未知，未知不能标红
async def _canBrowseOn(lockdown, bundleId):
    house = None
    errContainer = None
    try:
        try:
            house = await HouseArrestService.create(lockdown, bundleId, documents_only=False)
        except Exception as ex:
            errContainer = ex
            house = await HouseArrestService.create(lockdown, bundleId, documents_only=True)
        try:
            await house.listdir("/")
            return True
        except Exception as ex:
            if not _isPermDenied(ex):
                raise
        for name in ("Documents", "Library", "tmp", "StoreKit"):
            try:
                await house.listdir("/" + name)
                return True
            except Exception as ex:
                if _isConnectionError(ex):
                    raise
        return False
    except Exception as ex:
        # 某些 iOS App 拒绝 Vend 时只关闭该服务；两种服务都关闭但锁屏连接正常，表示该 App 当前不可访问
        if isinstance(errContainer, ConnectionTerminatedError) and isinstance(ex, ConnectionTerminatedError):
            try:
                if await lockdown.get_value(key="DeviceName"):
                    return False
            except Exception:
                logging.getLogger("IosFileTool").exception("检测 App 时手机连接已失效 app=%s", bundleId)
        text = str(ex).lower()
        if not _isConnectionError(ex) and ("refused to open the container" in text or "no app with bundle id" in text):
            return False
        logging.getLogger("IosFileTool").exception("检测结果未知，不标记无权限 app=%s", bundleId)
        return None
    finally:
        if house is not None:
            try:
                await house.close()
            except Exception:
                logging.getLogger("IosFileTool").exception("关闭 App 探测服务失败 app=%s", bundleId)


# 能浏览的在前，还没检测的居中，确认打不开的在最后
def _sortApps(tlTmApp):
    tlTmApp.sort(key=lambda tm: (
        0 if tm.get("browseKnown") and tm.get("canBrowse") else (2 if tm.get("browseKnown") else 1),
        0 if tm["hasIcon"] else 1,
        0 if tm["appType"] == "User" else 1,
        tm["name"].lower(),
        tm["bundleId"],
    ))


# 读 bundleId -> 能否浏览
def _readBrowse(path):
    tm = {}
    if not os.path.isfile(path):
        return tm
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "\t" not in line:
                continue
            bundleId, flag = line.split("\t", 1)
            tm[bundleId] = flag == "1"
    return tm


_browseWriteLock = threading.Lock()


# 写回能否浏览
def _writeBrowse(path, tmKnown):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for bundleId in sorted(tmKnown):
            f.write("%s\t%s\n" % (bundleId, "1" if tmKnown[bundleId] else "0"))


# 权限不够时的 AFC 状态，根目录被拒就改走子目录
def _isPermDenied(ex):
    text = str(ex)
    return "PERM_DENIED" in text or "status: 10" in text


# 顶层目录项，大小等进入后再读
def _dirEntry(name, remotePath):
    return {"name": name, "remotePath": remotePath, "isDir": True, "size": 0, "blocked": False}
