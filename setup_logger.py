import logging
import os
import sys


def setup_logger(log_level=logging.INFO):
    debug = os.environ.get('FMD_DEBUG', False)
    root = logging.getLogger()
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    if not debug:
        root.setLevel(log_level)
        handler.setLevel(log_level)
    else:
        logging.basicConfig(level=logging.DEBUG)
        handler.setLevel(logging.DEBUG)
    root.addHandler(handler)
