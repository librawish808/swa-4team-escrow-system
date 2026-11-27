"""HTTP API with injectable authentication and external adapters."""

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
import psycopg

from app import transaction_service as service
from app.external_ports import ExternalPort


def current_actor() -> int:
    raise HTTPException(503, "Authentication adapter is not configured")


def external_port() -> ExternalPort:
    raise HTTPException(503, "Bank/carrier adapter is not configured")


class OperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1, strict=True, examples=[1])


class WaybillRequest(OperationRequest):
    waybill_no: str = Field(min_length=1, max_length=100, pattern=r"\S", examples=["TEST123"])


class OperationResponse(BaseModel):
    id: int
    status: str
    version: int
    amount: int
    external_result: str
    recovery_required: bool
    idempotency_key: str


def create_app(actor_dependency=current_actor) -> FastAPI:
    app = FastAPI(title="Escrow transaction API")

    @app.exception_handler(service.TransactionConflict)
    async def conflict_handler(request, exc):
        return JSONResponse(status_code=409, content={"detail": "거래 상태·버전 또는 요청 권한을 확인하세요."})

    @app.exception_handler(psycopg.Error)
    async def database_error_handler(request, exc):
        # A commit may have succeeded. Do not expose SQL or encourage blind retry.
        return JSONResponse(status_code=503, content={
            "detail": "DB 처리 결과를 확인할 수 없습니다. 재요청 전에 거래 상태를 확인하세요."
        })

    @app.get("/")
    def root():
        return {"message": "Backend running"}

    @app.post("/transactions/{transaction_id}/waybill", response_model=OperationResponse,
              responses={202: {"description": "처리 중: 복구 확인 필요"}, 409: {"description": "거래 충돌"}})
    def waybill(
        transaction_id: Annotated[int, Path(ge=1)], body: WaybillRequest,
        actor: Annotated[int, Depends(actor_dependency)],
        port: Annotated[ExternalPort, Depends(external_port)],
    ):
        result = service.register_waybill(transaction_id, body.version, actor, body.waybill_no, port)
        return JSONResponse(status_code=202, content=result) if result["recovery_required"] else result

    @app.post("/transactions/{transaction_id}/cancel", response_model=OperationResponse,
              responses={202: {"description": "처리 중: 복구 확인 필요"}, 409: {"description": "거래 충돌"}})
    def cancel(
        transaction_id: Annotated[int, Path(ge=1)], body: OperationRequest,
        actor: Annotated[int, Depends(actor_dependency)],
        port: Annotated[ExternalPort, Depends(external_port)],
    ):
        result = service.cancel_transaction(transaction_id, body.version, actor, port)
        return JSONResponse(status_code=202, content=result) if result["recovery_required"] else result

    return app


app = create_app()
