# -*- coding: utf-8 -*-
"""Continuous Operation Polling Worker Daemon for Odoo Online."""
import logging
import time
from integration_service.operation_worker import OperationWorker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOGGER = logging.getLogger("operation_daemon")

def main():
    LOGGER.info("Starting M365 Operation Worker Daemon (polling interval: 10s)...")
    worker = OperationWorker()
    while True:
        try:
            pending = worker.fetch_pending_operations()
            if pending:
                LOGGER.info("Found %d pending operation(s) in Odoo Online", len(pending))
                for op in pending:
                    worker.process_operation(op)
            else:
                LOGGER.debug("No pending operations found")
        except Exception as exc:
            LOGGER.error("Error in operation worker loop: %s", exc)
        time.sleep(10)

if __name__ == "__main__":
    main()
