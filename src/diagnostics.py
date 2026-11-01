# -*- coding: utf-8 -*-

import logging
import os
from logging.handlers import RotatingFileHandler


# 界面与后台共用日志；失败堆栈包含线程和路径，单文件最多 2 MB
def configureLogging():
    logger = logging.getLogger("IosFileTool")
    if logger.handlers:
        return logger
    folder = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache", "logs")
    os.makedirs(folder, exist_ok=True)
    handler = RotatingFileHandler(os.path.join(folder, "IosFileTool.log"), maxBytes=2 * 1024 * 1024,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
