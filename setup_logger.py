import logging
import sys


def setup_logger(log_level=logging.INFO, log_file="app.log"):
    """
    Setup logging for the application to both stdout and a file.
    """
    logger = logging.getLogger()

    # Prevent duplicate handlers if setup_logger is called multiple times
    if not logger.handlers:
        logger.setLevel(log_level)

        # 1. Common Formatter
        formatter = logging.Formatter('%(asctime)s - %(processName)s/%(process)d - %(levelname)s - %(message)s')

        # 2. Console Handler (stdout)
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setLevel(log_level)
        stdout_handler.setFormatter(formatter)
        logger.addHandler(stdout_handler)

        # 3. File Handler
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)