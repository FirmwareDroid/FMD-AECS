import logging
import os
import sys


def setup_logger(logger_name=None, log_file=None, log_level=logging.INFO):
    """
    Configures a logger. Automatically creates missing log directories.
    """
    logger = logging.getLogger(logger_name)

    if not logger.handlers:
        logger.setLevel(log_level)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

        if log_file:
            log_dir = os.path.dirname(log_file)
            if log_dir:  # Only attempt if there's a directory path involved
                os.makedirs(log_dir, exist_ok=True)

        if logger_name is None:
            stdout_handler = logging.StreamHandler(sys.stdout)
            stdout_handler.setFormatter(formatter)
            logger.addHandler(stdout_handler)

            if log_file:
                file_handler = logging.FileHandler(log_file, encoding='utf-8')
                file_handler.setFormatter(formatter)
                logger.addHandler(file_handler)

        else:
            if log_file:
                sub_file_handler = logging.FileHandler(log_file, encoding='utf-8')
                sub_file_handler.setFormatter(formatter)
                logger.addHandler(sub_file_handler)

            logger.propagate = True

    return logger