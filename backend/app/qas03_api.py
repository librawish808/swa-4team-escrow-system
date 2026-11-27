"""QAS-03 가용성·장애 복구 데모 API (/api/qas-03/...).

main.py에는 include_router 한 줄로 붙는다. 다른 QAS 브랜치의 API와 경로가 겹치지 않는다.
데모 환경은 첫 요청 때 만들어지고, 그때 복구 작업(ExpirationJob)도 시작된다.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .external.mock_bank import BankFault
from .qas03_demo import DemoRuntime, run_qas03
from .transaction.service import TransactionNotFound

router = APIRouter(prefix="/api/qas-03", tags=["QAS-03 가용성·장애 복구"])

_runtime: Optional[DemoRuntime] = None
_runtime_lock = threading.Lock()


def runtime() -> DemoRuntime:
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            path = os.environ.get(
                "QAS03_DB_PATH", str(Path(__file__).resolve().parents[1] / "data" / "qas03.sqlite3")
            )
            _runtime = DemoRuntime(path)
        return _runtime


class CreateBody(BaseModel):
    stage: Literal["WAYBILL_REGISTERED", "DEPOSIT_CONFIRMED"] = "WAYBILL_REGISTERED"


class FaultBody(BaseModel):
    fault: Literal["NONE", "DOWN", "SERVER_ERROR", "RESPONSE_LOST"]


class CrashBody(BaseModel):
    after_send: bool = False


def _call(fn, *args):
    try:
        return fn(*args)
    except TransactionNotFound:
        raise HTTPException(404, "거래를 찾을 수 없습니다.")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(409, str(e))


@router.get("/state")
def get_state():
    return runtime().state()


@router.post("/transactions", status_code=201)
def create_transaction(body: CreateBody):
    return _call(runtime().create_transaction, body.stage)


@router.post("/bank-fault")
def set_bank_fault(body: FaultBody):
    runtime().set_bank_fault(BankFault(body.fault))
    return {"bank_fault": body.fault}


@router.post("/transactions/{transaction_id}/confirm-purchase")
def confirm_purchase(transaction_id: str):
    return _call(runtime().confirm_purchase, transaction_id)


@router.post("/transactions/{transaction_id}/cancel")
def cancel(transaction_id: str):
    return _call(runtime().cancel, transaction_id)


@router.post("/transactions/{transaction_id}/crash-restart")
def crash_restart(transaction_id: str, body: CrashBody):
    _call(runtime().crash_and_restart, transaction_id, body.after_send)
    return {"restarted": True}


@router.post("/recovery/run")
def run_recovery():
    return runtime().run_recovery_now()


@router.post("/reset")
def reset():
    runtime().reset()
    return {"reset": True}


@router.post("/benchmark")
def benchmark():
    rt = runtime()
    if not rt.benchmark_lock.acquire(blocking=False):
        raise HTTPException(409, "100건 검증이 이미 실행 중입니다.")
    try:
        return run_qas03()
    finally:
        rt.benchmark_lock.release()
