import logging
import sys


def setup_logger(log_level=logging.INFO):
    """
    Setup logging for the application.
    """
    logger = logging.getLogger()
    if not logger.handlers:
        logger.setLevel(log_level)
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(log_level)
        formatter = logging.Formatter(f'%(asctime)s - %(processName)s/%(process)d - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)