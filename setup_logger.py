import logging
import sys


def setup_logger(logger_name=None, log_file=None, log_level=logging.INFO):
    """
    Configures a logger. If logger_name is None, configures the root logger.
    """
    logger = logging.getLogger(logger_name)

    # Prevent adding duplicate handlers if initialized multiple times
    if not logger.handlers:
        logger.setLevel(log_level)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

        # If it's the root logger, give it stdout and a main file
        if logger_name is None:
            stdout_handler = logging.StreamHandler(sys.stdout)
            stdout_handler.setFormatter(formatter)
            logger.addHandler(stdout_handler)

            if log_file:
                file_handler = logging.FileHandler(log_file, encoding='utf-8')
                file_handler.setFormatter(formatter)
                logger.addHandler(file_handler)

        # If it's a sub-logger, give it its own dedicated file
        else:
            if log_file:
                sub_file_handler = logging.FileHandler(log_file, encoding='utf-8')
                sub_file_handler.setFormatter(formatter)
                logger.addHandler(sub_file_handler)

            # Optional: Set propagate to True if you want sub-script logs
            # to also show up in the main script's stdout/file.
            # Set to False if you want them ONLY in the sub-script file.
            logger.propagate = True

    return logger