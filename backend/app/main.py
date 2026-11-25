import asyncio
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .adapters import MockBankPort, MockDeliveryPort
from .benchmark import run_qas01
from .domain import DomainError
from .repository import SqlTransactionRepository
from .runner import DeadlineRecoveryRunner
from .service import TransactionService


class DemoTransaction(BaseModel):
    deadline_in_seconds: float = Field(default=15, ge=1, le=300)


class ReturnWaybill(BaseModel):
    actor_id: str = Field(min_length=1, max_length=100)
    request_id: str = Field(min_length=1, max_length=100)
    waybill_no: str = Field(min_length=1, max_length=100, pattern=r"^[A-Z0-9-]+$")


def create_app(database_path=None, carrier_delay_seconds=3.0):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        path = database_path or os.environ.get(
            "ESCROW_DB_PATH",
            str(Path(__file__).resolve().parents[1] / "data" / "escrow.sqlite3"),
        )
        repository = SqlTransactionRepository(path)
        service = TransactionService(
            repository,
            MockDeliveryPort(carrier_delay_seconds),
            MockBankPort(repository),
        )
        runner = DeadlineRecoveryRunner(service)
        app.state.service = service
        app.state.benchmark_lock = asyncio.Lock()
        await runner.start()
        try:
            yield
        finally:
            await runner.stop()

    app = FastAPI(title="Escrow QAS-01 reliability demo", lifespan=lifespan)

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, error: DomainError):
        return JSONResponse(
            status_code=error.status_code, content={"detail": str(error)}
        )

    @app.get("/")
    def root():
        return {"message": "Backend running", "scenario": "QAS-01", "mode": "demo"}

    @app.get("/api/transactions")
    def list_transactions(request: Request):
        service = request.app.state.service
        return [
            service.get_transaction_status(i) for i in service.repository.list_ids()
        ]

    @app.post("/api/demo/transactions", status_code=201)
    def create_demo_transaction(body: DemoTransaction, request: Request):
        service = request.app.state.service
        notice_at = (
            service.clock()
            + timedelta(seconds=body.deadline_in_seconds)
            - timedelta(hours=72)
        )
        transaction_id = service.repository.create_approved_return(notice_at)
        return service.get_transaction_status(transaction_id)

    @app.get("/api/transactions/{transaction_id}")
    def get_transaction(transaction_id: str, actor_id: str, request: Request):
        return request.app.state.service.get_transaction_status(
            transaction_id, actor_id
        )

    @app.post("/api/transactions/{transaction_id}/return-waybills", status_code=202)
    def register_waybill(transaction_id: str, body: ReturnWaybill, request: Request):
        service = request.app.state.service
        service.register_return_waybill(
            transaction_id, body.actor_id, body.request_id, body.waybill_no
        )
        return service.get_transaction_status(transaction_id, body.actor_id)

    @app.post("/api/demo/transactions/{transaction_id}/arrive")
    def mock_return_arrival(transaction_id: str, request: Request):
        service = request.app.state.service
        service.mark_mock_arrived(transaction_id)
        return service.get_transaction_status(transaction_id)

    @app.post("/api/demo/qas-01")
    async def benchmark(request: Request):
        lock = request.app.state.benchmark_lock
        if lock.locked():
            raise DomainError("100건 검증이 이미 실행 중입니다.")
        async with lock:
            # Keep synchronous benchmark setup away from the live deadline loop.
            return await asyncio.to_thread(lambda: asyncio.run(run_qas01()))

    return app


app = create_app()
