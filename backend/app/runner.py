"""Same-process deadline and external lookup execution, with separate tasks."""

import asyncio
import logging

from .service import TransactionService

logger = logging.getLogger(__name__)


class DeadlineRecoveryRunner:
    def __init__(self, service: TransactionService, verification_workers=4):
        self.service = service
        self.verification_workers = verification_workers
        self.tasks = []

    async def start(self):
        self.tasks = [
            asyncio.create_task(self.deadlines()),
            asyncio.create_task(self.transfers()),
        ]
        self.tasks.extend(
            asyncio.create_task(self.verifications())
            for _ in range(self.verification_workers)
        )

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    async def deadlines(self):
        while True:
            try:
                await asyncio.to_thread(self.service.process_due_deadlines)
            except Exception:
                logger.exception(
                    "Deadline processing failed; retaining persisted work for next tick"
                )
            await asyncio.sleep(0.05)

    async def verifications(self):
        while True:
            try:
                receipt = await asyncio.to_thread(
                    self.service.repository.claim_receipt, self.service.clock()
                )
                if receipt:
                    await self.service.verify_receipt(receipt)
                    continue
            except Exception:
                logger.exception(
                    "Carrier lookup failed; receipt remains recoverable after lease expiry"
                )
            await asyncio.sleep(0.1)

    async def transfers(self):
        while True:
            try:
                await self.service.dispatch_transfers()
            except Exception:
                logger.exception(
                    "Mock transfer failed; retrying persisted instruction with same key"
                )
            await asyncio.sleep(0.1)
