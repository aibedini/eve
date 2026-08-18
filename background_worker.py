"""Dedicated runtime for refresh, scheduler and automation workers."""

import os
import signal
import time

os.environ.setdefault('EVE_PROCESS_ROLE', 'background')
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from panel.core.logging_config import setup_logging, get_logger

setup_logging()
logger = get_logger(__name__)

from app import ensure_background_threads_started, get_redis


def main():
    if get_redis() is None:
        raise RuntimeError('The background process requires REDIS_URL')

    ensure_background_threads_started()
    stopped = False

    def _stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    logger.info('[ProcessRole] dedicated background process started (PID=%s)', os.getpid())
    while not stopped:
        time.sleep(1)


if __name__ == '__main__':
    main()
