# 20230629
import os
import logging
import pathlib
import coloredlogs
from logging import handlers
from config import getInstance

from concurrent_log_handler import ConcurrentRotatingFileHandler


def get_logger(name) -> logging.Logger:
    """
    生成logger
    :param name: 日志文件名
    :return:
    """
    log_dir = pathlib.Path().joinpath("logs")
    log_file = log_dir.joinpath(f"{name}.log")
    err_log_file = f"{log_dir}/error.log"

    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    # 终端通道
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    # 文件通道
    fh = handlers.RotatingFileHandler(mode='a',
                                      encoding='utf-8',
                                      filename=log_file,
                                      backupCount=8,
                                      maxBytes=8*1024*1024)
    efh = ConcurrentRotatingFileHandler(mode='a',
                                        encoding='utf-8',
                                        filename=err_log_file,
                                        backupCount=8,
                                        maxBytes=8*1024*1024)
    fh.setLevel(logging.DEBUG)
    efh.setLevel(logging.WARN)
    # 格式
    color_formatter = coloredlogs.ColoredFormatter(
        '%(asctime)s - %(module)-8s - %(funcName)-14s[line:%(lineno)3d] - %(levelname)-8s: %(message)s')

    bare_formatter = logging.Formatter(
        '%(asctime)s - %(module)-8s - %(funcName)-14s[line:%(lineno)3d] - %(levelname)-8s: %(message)s')
    ch.setFormatter(color_formatter)
    fh.setFormatter(bare_formatter)
    efh.setFormatter(bare_formatter)
    logger.addHandler(ch)
    logger.addHandler(fh)
    logger.addHandler(efh)
    return logger
