"""QAS-03 가용성·장애 복구 데모 실행 상태 (FastAPI 라우터와 100건 검증에서 사용).

웹 화면 한 개가 하나의 데모 환경을 공유한다.
- Mock 은행·택배는 메모리에 있고, '서버 재시작'을 눌러도 살아 있다 (외부 시스템이므로).
- 거래 DB는 SQLite 파일이라 재시작 후에도 남는다.
- 실제로 uvicorn을 다시 띄우면 Mock 은행이 초기화되므로 데모 DB도 함께 비운다.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .bootstrap import EscrowSystem, build_system
from .external.mock_bank import BankFault, MockBankServer
from .external.mock_courier import MockCourierServer
from .external.ports import BankPort
from .policy.policy import SimpleTransactionPolicy
from .recovery.expiration_job import ExpirationJob
from .transaction.models import Transaction, TransactionStatus as S
from .transaction.service import RecoveryReport, ServiceResult, TransactionService

log = logging.getLogger(__name__)

SELLER = "seller-demo"
BUYER = "buyer-demo"
AMOUNT = 100_000

# (구매자 화면, 판매자 화면) 표시 문구
VIEW_LABELS = {
    S.WAITING_BUYER: ("구매자 참여 대기", "구매자 참여 대기"),
    S.WAITING_DEPOSIT: ("입금 대기", "입금 대기"),
    S.DEPOSIT_CONFIRMED: ("입금 완료 · 발송 대기", "입금 확인 · 발송해 주세요"),
    S.WAYBILL_REGISTERED: ("배송 중 · 구매확정 대기", "배송 중 · 대금 보관 중"),
    S.PAYOUT_PROCESSING: ("구매확정 완료 · 지급 확인 중", "지급 확인 중"),
    S.COMPLETED: ("거래 완료", "지급 완료"),
    S.REFUND_PROCESSING: ("취소 요청 · 환불 확인 중", "거래 취소 · 환불 확인 중"),
    S.CANCELLED: ("환불 완료", "거래 취소됨"),
}

CODE_MESSAGES = {
    "COMPLETED": "지급 완료 확정",
    "CANCELLED": "환불 완료 확정",
    "PROCESSING": "은행 결과 미확인 → 처리 중 유지",
    "TRANSFER_FAILED": "은행 거절 → 담당자 확인 필요",
    "DUPLICATE_REQUEST": "중복 요청 차단",
}


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def now_iso() -> str:
    return iso(datetime.now(timezone.utc))


class SimulatedCrash(Exception):
    """은행 호출 도중 서버 프로세스가 비정상 종료됨."""


class CrashingBankPort(BankPort):
    """지급·환불 요청 시점에 서버가 죽는 상황을 흉내 낸다.

    crash_after_send=False: 은행에 보내기 직전에 종료 (은행은 요청을 모름)
    crash_after_send=True : 은행이 처리한 뒤, 응답을 받기 전에 종료
    """

    def __init__(self, inner: BankPort, crash_after_send: bool) -> None:
        self.inner = inner
        self.crash_after_send = crash_after_send

    def check_deposit(self, transaction_id):
        return self.inner.check_deposit(transaction_id)

    def get_transfer_result(self, request_key):
        return self.inner.get_transfer_result(request_key)

    def _crash(self, send):
        if self.crash_after_send:
            send()
        raise SimulatedCrash()

    def request_payout(self, transaction_id, request_key):
        self._crash(lambda: self.inner.request_payout(transaction_id, request_key))

    def request_refund(self, transaction_id, request_key):
        self._crash(lambda: self.inner.request_refund(transaction_id, request_key))


def prepare_transaction(system: EscrowSystem, stage: str = "WAYBILL_REGISTERED") -> Transaction:
    """정상 상태에서 거래를 stage까지 진행시킨다 (등록 → 참여 → 입금 확인 → 운송장)."""
    s = system.service
    tx = s.create_transaction(SELLER, AMOUNT)
    s.join_transaction(tx.id, BUYER, tx.invite_token)
    system.bank_server.make_deposit(tx.id, AMOUNT)
    r = s.confirm_deposit(tx.id)
    if r.code != "DEPOSIT_CONFIRMED":
        raise RuntimeError(f"입금 확인 실패: {r.code}")
    if stage == "WAYBILL_REGISTERED":
        waybill = f"WB-{tx.id[:8].upper()}"
        system.courier_server.add_shipment(waybill, recipient=BUYER)
        r = s.register_waybill(tx.id, SELLER, waybill)
        if r.code != "WAYBILL_REGISTERED":
            raise RuntimeError(f"운송장 등록 실패: {r.code}")
    return system.repository.find_by_id(tx.id)


def crash_during_transfer(system: EscrowSystem, transaction_id: str, after_send: bool) -> None:
    """구매확정(또는 환불) 도중 서버가 죽는 상황을 재현한다. 호출 전 상태 저장까지는 진행된다."""
    s = system.service
    crashing = TransactionService(
        system.repository, s.policy, system.guard, CrashingBankPort(s.bank, after_send), s.delivery
    )
    tx = system.repository.find_by_id(transaction_id)
    try:
        if tx.status == S.DEPOSIT_CONFIRMED:
            crashing.cancel_transaction(transaction_id, BUYER)
        else:
            crashing.confirm_purchase(transaction_id, BUYER)
    except SimulatedCrash:
        return
    raise RuntimeError("재현할 이체 요청이 없습니다 (이미 처리 중이거나 완료된 거래)")


class DemoRuntime:
    def __init__(self, db_path: str, timeout_millis: int = 3000, job_interval_seconds: float = 2.0) -> None:
        self.db_path = db_path
        self.timeout_millis = timeout_millis
        self.job_interval_seconds = job_interval_seconds
        self.bank = MockBankServer()
        self.courier = MockCourierServer()
        self.events: List[dict] = []
        self.restarts = 0
        self.lock = threading.RLock()
        self.benchmark_lock = threading.Lock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.system = self._boot()
        self.system.repository.clear()  # Mock 은행이 새로 시작되므로 거래 DB도 비움
        self.system.guard.clear()

    # ---------- 내부 ----------
    def _boot(self) -> EscrowSystem:
        system = build_system(
            db_path=self.db_path,
            bank_server=self.bank,
            courier_server=self.courier,
            timeout_millis=self.timeout_millis,
            job_interval_seconds=self.job_interval_seconds,
        )
        system.job.on_report = self._on_job_report
        system.job.start()  # 시작 직후 1회 실행 후 주기 실행
        return system

    def _event(self, message: str, level: str = "info", transaction_id: Optional[str] = None) -> None:
        self.events.append({"at": now_iso(), "message": message, "level": level, "transaction_id": transaction_id})
        del self.events[:-200]

    def _on_job_report(self, report: RecoveryReport) -> None:
        if report.resent:
            self._event(f"[복구 작업] 은행에 기록 없는 요청 {report.resent}건을 같은 requestKey로 재요청", "info")
        for tx_id, kind, code in report.outcomes:
            label = "지급" if kind == "PAYOUT" else "환불"
            message = f"[복구 작업] {tx_id[:8]} {label}: {CODE_MESSAGES.get(code, code)}"
            last = next((e for e in reversed(self.events) if e["transaction_id"] == tx_id), None)
            if last and last["message"] == message:
                continue  # 같은 결과 반복은 기록하지 않음
            level = "success" if code in ("COMPLETED", "CANCELLED") else "warn"
            self._event(message, level, tx_id)

    def _service_result(self, r: ServiceResult, elapsed_ms: float) -> dict:
        return {"code": r.code, "status": r.status.value, "message": r.message, "elapsed_ms": round(elapsed_ms, 1)}

    # ---------- 화면 조작 ----------
    def create_transaction(self, stage: str) -> dict:
        with self.lock:
            fault = self.bank.fault
            self.bank.set_fault(BankFault.NONE)  # 준비 단계(입금 확인)는 정상 은행에서 진행
            try:
                tx = prepare_transaction(self.system, stage)
            finally:
                self.bank.set_fault(fault)
            label = "운송장 등록 완료" if stage == "WAYBILL_REGISTERED" else "입금 확인 완료"
            self._event(f"거래 {tx.id[:8]} 생성 · {AMOUNT:,}원 · {label}", "info", tx.id)
            return {"id": tx.id}

    def set_bank_fault(self, fault: BankFault) -> None:
        self.bank.set_fault(fault)
        text = {
            BankFault.NONE: "은행 정상화",
            BankFault.DOWN: "은행 장애 주입: 연결 실패 (요청이 은행에 도달하지 않음)",
            BankFault.SERVER_ERROR: "은행 장애 주입: 5xx 오류 (요청이 처리되지 않음)",
            BankFault.RESPONSE_LOST: "은행 장애 주입: 응답 유실 (은행은 처리, 응답이 타임아웃 뒤 도착)",
        }[fault]
        self._event(text, "success" if fault == BankFault.NONE else "warn")

    def confirm_purchase(self, transaction_id: str) -> dict:
        start = time.perf_counter()
        r = self.system.service.confirm_purchase(transaction_id, BUYER)
        elapsed = (time.perf_counter() - start) * 1000
        self._event(f"구매확정 {transaction_id[:8]}: {CODE_MESSAGES.get(r.code, r.code)} ({elapsed:,.0f}ms)",
                    "success" if r.code == "COMPLETED" else "warn", transaction_id)
        return self._service_result(r, elapsed)

    def cancel(self, transaction_id: str) -> dict:
        start = time.perf_counter()
        r = self.system.service.cancel_transaction(transaction_id, BUYER)
        elapsed = (time.perf_counter() - start) * 1000
        self._event(f"거래 취소 {transaction_id[:8]}: {CODE_MESSAGES.get(r.code, r.message or r.code)} ({elapsed:,.0f}ms)",
                    "success" if r.code == "CANCELLED" else "warn", transaction_id)
        return self._service_result(r, elapsed)

    def crash_and_restart(self, transaction_id: str, after_send: bool) -> None:
        with self.lock:
            crash_during_transfer(self.system, transaction_id, after_send)
            where = "은행 처리 후 응답 수신 전" if after_send else "은행 호출 직전"
            self._event(f"서버 비정상 종료 ({where}) · 거래 {transaction_id[:8]}", "error", transaction_id)
            self.system.shutdown()
            self.restarts += 1
            self.system = self._boot()
            self._event("서버 재시작 · DB에 남은 처리 중 거래를 복구 작업이 즉시 확인", "info")

    def run_recovery_now(self) -> dict:
        report = self.system.job.run()
        if not report.checked:
            self._event("[복구 작업] 처리 중인 거래 없음", "info")
        return self._report_dict(report)

    def reset(self) -> None:
        with self.lock:
            self.system.shutdown()
            self.bank = MockBankServer()
            self.courier = MockCourierServer()
            self.system = self._boot()
            self.system.repository.clear()
            self.system.guard.clear()
            self.events.clear()
            self.restarts = 0
            self._event("데모 초기화", "info")

    # ---------- 화면 조회 ----------
    @staticmethod
    def _report_dict(report: Optional[RecoveryReport]) -> Optional[dict]:
        if report is None:
            return None
        d = asdict(report)
        d.pop("details", None)
        d.pop("outcomes", None)
        return d

    def state(self) -> dict:
        job: ExpirationJob = self.system.job
        transfers_by_tx: Dict[str, List[dict]] = {}
        transfers = self.bank.all_transfers()
        for t in transfers:
            transfers_by_tx.setdefault(t["transactionId"], []).append(t)
        txs = []
        for tx in self.system.repository.find_all():
            buyer_view, seller_view = VIEW_LABELS[tx.status]
            done = [t for t in transfers_by_tx.get(tx.id, []) if t["status"] == "DONE"]
            txs.append({
                "id": tx.id,
                "amount": tx.amount,
                "status": tx.status.value,
                "buyer_view": buyer_view,
                "seller_view": seller_view,
                "version": tx.version,
                "payout_key": tx.payout_request_key,
                "refund_key": tx.refund_request_key,
                "bank_executed": len(done),
                "can_confirm": tx.status == S.WAYBILL_REGISTERED,
                "can_cancel": tx.status == S.DEPOSIT_CONFIRMED,
                "processing": tx.status in (S.PAYOUT_PROCESSING, S.REFUND_PROCESSING),
            })
        return {
            "server_now": now_iso(),
            "bank_fault": self.bank.fault.value,
            "timeout_millis": self.timeout_millis,
            "job": {
                "interval_seconds": self.job_interval_seconds,
                "last_run_at": iso(job.last_run_at) if job.last_run_at else None,
                "last_report": self._report_dict(job.last_report),
                "restarts": self.restarts,
            },
            "transactions": txs,
            "transfers": [
                {
                    "request_key": t["requestKey"],
                    "kind": t["kind"],
                    "transaction_id": t["transactionId"],
                    "amount": t["amount"],
                    "status": "SUCCEEDED" if t["status"] == "DONE" else "FAILED",
                    "attempts": t["attempts"],
                }
                for t in reversed(transfers)
            ],
            "events": list(reversed(self.events[-80:])),
        }


# ---------------- 100건 자동 검증 ----------------
def run_qas03(runs: int = 100, crash_cases: int = 20, timeout_millis: int = 50) -> dict:
    """QAS-AV-01·AV-02 측정 기준을 별도 임시 DB에서 재현한다. 화면의 데모 거래에는 영향 없음."""
    with tempfile.TemporaryDirectory(prefix="qas03-") as directory:
        db = os.path.join(directory, "qas03-benchmark.sqlite3")
        bank = MockBankServer()
        courier = MockCourierServer()
        system = build_system(db_path=db, bank_server=bank, courier_server=courier, timeout_millis=timeout_millis)
        try:
            # ---- AV-01: 외부 장애 ----
            txs = [prepare_transaction(system) for _ in range(runs)]
            response_ms: List[float] = []
            held = 0
            for i, tx in enumerate(txs):
                bank.set_fault(BankFault.RESPONSE_LOST if i % 2 == 0 else BankFault.DOWN)
                start = time.perf_counter()
                r = system.service.confirm_purchase(tx.id, BUYER)
                response_ms.append((time.perf_counter() - start) * 1000)
                held += r.status == S.PAYOUT_PROCESSING and r.code == "PROCESSING"

            duplicates_blocked = sum(
                system.service.confirm_purchase(tx.id, BUYER).code == "DUPLICATE_REQUEST" for tx in txs
            )
            # 장애 중 은행과 무관한 기능 (UC1 등록, UC2 참여, UC7 조회)
            other_ok = 0
            for _ in range(10):
                t = system.service.create_transaction(SELLER, AMOUNT)
                ok = system.service.join_transaction(t.id, BUYER, t.invite_token).code == "JOINED"
                ok = ok and system.service.get_transaction_status(t.id, BUYER).code == "OK"
                other_ok += ok

            bank.set_fault(BankFault.NONE)
            start = time.perf_counter()
            report = system.job.run()
            recovery_ms = (time.perf_counter() - start) * 1000
            completed = sum(system.repository.find_by_id(tx.id).status == S.COMPLETED for tx in txs)
            executed = [len(bank.executed_transfers(tx.id, "PAYOUT")) for tx in txs]

            # ---- AV-02: 내부 장애 (은행 호출 중 서버 종료 → 재시작) ----
            crash_txs = [prepare_transaction(system) for _ in range(crash_cases)]
            for i, tx in enumerate(crash_txs):
                crash_during_transfer(system, tx.id, after_send=i % 2 == 1)
            system.shutdown()
            system = build_system(db_path=db, bank_server=bank, courier_server=courier, timeout_millis=timeout_millis)
            left_processing = len(system.repository.find_by_status([S.PAYOUT_PROCESSING]))
            start = time.perf_counter()
            crash_report = system.job.run()
            restart_recovery_ms = (time.perf_counter() - start) * 1000
            crash_completed = sum(system.repository.find_by_id(tx.id).status == S.COMPLETED for tx in crash_txs)
            crash_executed = [len(bank.executed_transfers(tx.id, "PAYOUT")) for tx in crash_txs]

            max_response = max(response_ms)
            duplicate_payouts = sum(max(0, n - 1) for n in executed + crash_executed)
            result = {
                "runs": runs,
                "timeout_millis": timeout_millis,
                "held_processing": held,
                "duplicates_blocked": duplicates_blocked,
                "other_features_ok": other_ok,
                "other_features_total": 10,
                "max_response_ms": round(max_response, 1),
                "recovered": completed,
                "resent": report.resent,
                "recovery_ms": round(recovery_ms, 1),
                "crash_cases": crash_cases,
                "crash_left_processing": left_processing,
                "crash_recovered": crash_completed,
                "crash_resent": crash_report.resent,
                "restart_recovery_ms": round(restart_recovery_ms, 1),
                "duplicate_payouts": duplicate_payouts,
                "missing_payouts": sum(n == 0 for n in executed + crash_executed),
            }
            result["passed"] = (
                held == runs
                and duplicates_blocked == runs
                and other_ok == 10
                and max_response <= 10_000
                and completed == runs
                and recovery_ms <= 300_000
                and crash_completed == crash_cases
                and restart_recovery_ms <= 60_000
                and duplicate_payouts == 0
                and result["missing_payouts"] == 0
            )
            return result
        finally:
            system.shutdown()


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.ERROR)
    print(json.dumps(run_qas03(), ensure_ascii=False, indent=2))
