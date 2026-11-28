"""가용성 및 장애 복구성 QAS 검증 테스트 (QAS-AV-01 외부 장애, QAS-AV-02 내부 장애).

실행 (backend 폴더에서):  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import logging
import os
import tempfile
import time
import unittest

from app.bootstrap import build_system
from app.external.bank_adapter import BankAdapter
from app.external.bank_client import BankApiClient
from app.external.mock_bank import BankFault, MockBankServer
from app.transaction.models import TransactionStatus as S

from tests.helpers import AMOUNT, BUYER, SELLER, CrashingBankPort, SimulatedCrash, make_tx

logging.disable(logging.CRITICAL)  # 테스트 출력에서 의도된 장애 로그 숨김

TIMEOUT_MS = 50  # 테스트 속도를 위해 짧게 (실제 기본값 3000ms)
RESPONSE_TIME_LIMIT_S = 10.0  # QAS 측정 기준: 사용자 응답 10초 이내


def new_system(**kw):
    kw.setdefault("timeout_millis", TIMEOUT_MS)
    return build_system(**kw)


class ExternalBankFailureTest(unittest.TestCase):
    """QAS-AV-01: 지급·환불 중 은행 응답 없음."""

    def setUp(self):
        self.sys = new_system()
        self.bank = self.sys.bank_server
        self.svc = self.sys.service

    def tearDown(self):
        self.sys.shutdown()

    def test_bank_down_payout_stays_processing_and_responds_fast(self):
        tx = make_tx(self.sys)
        self.bank.set_fault(BankFault.DOWN)

        start = time.perf_counter()
        r = self.svc.confirm_purchase(tx.id, BUYER)
        elapsed = time.perf_counter() - start

        self.assertEqual(r.code, "PROCESSING")
        self.assertEqual(r.status, S.PAYOUT_PROCESSING)  # 완료로 표시하지 않음
        self.assertLess(elapsed, RESPONSE_TIME_LIMIT_S)
        self.assertEqual(len(self.bank.executed_transfers(tx.id)), 0)

    def test_response_lost_is_unknown_not_failed_and_never_double_pays(self):
        tx = make_tx(self.sys)
        self.bank.set_fault(BankFault.RESPONSE_LOST)

        r = self.svc.confirm_purchase(tx.id, BUYER)
        self.assertEqual(r.code, "PROCESSING")
        self.assertEqual(r.status, S.PAYOUT_PROCESSING)
        self.assertEqual(len(self.bank.executed_transfers(tx.id)), 1)  # 은행은 실제로 지급함

        for _ in range(5):  # 구매자가 답답해서 구매확정을 반복
            self.assertEqual(self.svc.confirm_purchase(tx.id, BUYER).code, "DUPLICATE_REQUEST")
        self.assertEqual(len(self.bank.executed_transfers(tx.id)), 1)

    def test_other_features_work_while_bank_down(self):
        ready = make_tx(self.sys, until="DEPOSIT_CONFIRMED")
        waiting = make_tx(self.sys, until="WAITING_DEPOSIT")
        self.bank.set_fault(BankFault.DOWN)

        # UC1 거래 등록, UC2 참여
        new_tx = self.svc.create_transaction(SELLER, AMOUNT)
        self.assertEqual(self.svc.join_transaction(new_tx.id, BUYER, new_tx.invite_token).code, "JOINED")
        # UC4 운송장 등록 (택배만 필요)
        self.sys.courier_server.add_shipment("WB-OK", recipient=BUYER)
        self.assertEqual(self.svc.register_waybill(ready.id, SELLER, "WB-OK").code, "WAYBILL_REGISTERED")
        # UC7 상태 조회
        self.assertEqual(self.svc.get_transaction_status(ready.id, BUYER).code, "OK")
        # UC3 입금 확인은 은행이 필요 → 상태 유지하고 안내만
        r = self.svc.confirm_deposit(waiting.id)
        self.assertEqual(r.code, "BANK_UNAVAILABLE")
        self.assertEqual(r.status, S.WAITING_DEPOSIT)

    def test_100_transactions_recovered_after_bank_restored_without_duplicates(self):
        txs = [make_tx(self.sys) for _ in range(100)]
        for i, tx in enumerate(txs):
            # 절반은 응답 유실(은행 처리됨), 절반은 연결 실패(은행 미처리)
            self.bank.set_fault(BankFault.RESPONSE_LOST if i % 2 == 0 else BankFault.DOWN)
            self.assertEqual(self.svc.confirm_purchase(tx.id, BUYER).code, "PROCESSING")

        self.bank.set_fault(BankFault.NONE)  # 은행 복구
        start = time.perf_counter()
        report = self.sys.job.run()
        elapsed = time.perf_counter() - start

        self.assertEqual(report.checked, 100)
        self.assertEqual(report.completed, 100)
        self.assertEqual(report.resent, 50)  # 은행이 못 받은 50건만 같은 키로 재요청
        self.assertLess(elapsed, 300)  # 측정 기준: 복구 후 5분 이내 확정
        for tx in txs:
            self.assertEqual(self.sys.repository.find_by_id(tx.id).status, S.COMPLETED)
            self.assertEqual(len(self.bank.executed_transfers(tx.id, "PAYOUT")), 1)  # 중복 지급 0건

    def test_recovery_while_bank_still_down_keeps_processing(self):
        tx = make_tx(self.sys)
        self.bank.set_fault(BankFault.DOWN)
        self.svc.confirm_purchase(tx.id, BUYER)

        report = self.sys.job.run()
        self.assertEqual(report.still_processing, 1)
        self.assertEqual(self.sys.repository.find_by_id(tx.id).status, S.PAYOUT_PROCESSING)

    def test_refund_response_lost_then_recovered(self):
        tx = make_tx(self.sys, until="DEPOSIT_CONFIRMED")
        self.bank.set_fault(BankFault.RESPONSE_LOST)
        r = self.svc.cancel_transaction(tx.id, BUYER)
        self.assertEqual((r.code, r.status), ("PROCESSING", S.REFUND_PROCESSING))
        self.assertEqual(self.svc.cancel_transaction(tx.id, BUYER).code, "DUPLICATE_REQUEST")

        self.bank.set_fault(BankFault.NONE)
        self.sys.job.run()
        self.assertEqual(self.sys.repository.find_by_id(tx.id).status, S.CANCELLED)
        self.assertEqual(len(self.bank.executed_transfers(tx.id, "REFUND")), 1)

    def test_bank_rejection_is_failed_and_not_completed(self):
        tx = make_tx(self.sys)
        self.bank._deposits.pop(tx.id)  # 은행 쪽에서 이체 불가 상황 (예: 계좌 오류)
        r = self.svc.confirm_purchase(tx.id, BUYER)
        self.assertEqual((r.code, r.status), ("TRANSFER_FAILED", S.PAYOUT_PROCESSING))


class LookupRetryTest(unittest.TestCase):
    """조회는 재시도, 끝까지 실패하면 UNKNOWN."""

    def setUp(self):
        self.sys = new_system(lookup_attempts=2)

    def tearDown(self):
        self.sys.shutdown()

    def test_transient_bank_failure_is_absorbed_by_retry(self):
        tx = make_tx(self.sys, until="WAITING_DEPOSIT")
        self.sys.bank_server.make_deposit(tx.id, AMOUNT)
        self.sys.bank_server.fail_next(1)
        self.assertEqual(self.sys.service.confirm_deposit(tx.id).code, "DEPOSIT_CONFIRMED")

    def test_persistent_bank_failure_returns_unknown_and_keeps_state(self):
        tx = make_tx(self.sys, until="WAITING_DEPOSIT")
        self.sys.bank_server.make_deposit(tx.id, AMOUNT)
        self.sys.bank_server.fail_next(2)
        r = self.sys.service.confirm_deposit(tx.id)
        self.assertEqual((r.code, r.status), ("BANK_UNAVAILABLE", S.WAITING_DEPOSIT))

    def test_courier_down_does_not_block_bank_features(self):
        a = make_tx(self.sys, until="DEPOSIT_CONFIRMED")
        b = make_tx(self.sys, until="WAITING_DEPOSIT")
        self.sys.courier_server.set_down(True)

        r = self.sys.service.register_waybill(a.id, SELLER, "WB-X")
        self.assertEqual((r.code, r.status), ("COURIER_UNAVAILABLE", S.DEPOSIT_CONFIRMED))
        self.sys.bank_server.make_deposit(b.id, AMOUNT)
        self.assertEqual(self.sys.service.confirm_deposit(b.id).code, "DEPOSIT_CONFIRMED")


class InternalCrashRecoveryTest(unittest.TestCase):
    """QAS-AV-02: 은행 응답 전에 서버 비정상 종료 → 재시작 후 복구."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.bank = MockBankServer()  # 외부 은행은 서버가 죽어도 계속 살아 있음

    def tearDown(self):
        os.remove(self.db)

    def _crash_during_payout(self, crash_after_send: bool) -> str:
        real_bank = BankAdapter(BankApiClient(self.bank, TIMEOUT_MS))
        first = new_system(db_path=self.db, bank_server=self.bank,
                           bank_port=CrashingBankPort(real_bank, crash_after_send))
        tx = make_tx(first)
        with self.assertRaises(SimulatedCrash):
            first.service.confirm_purchase(tx.id, BUYER)
        first.shutdown()  # 프로세스 종료: 메모리 상태 소멸, DB 파일만 남음
        return tx.id

    def _restart_and_recover(self, tx_id: str):
        second = new_system(db_path=self.db, bank_server=self.bank)
        try:
            # 재시작 직후: 처리 중 상태가 DB에 남아 있어야 복구할 수 있다
            self.assertEqual(second.repository.find_by_id(tx_id).status, S.PAYOUT_PROCESSING)
            start = time.perf_counter()
            report = second.job.run()
            self.assertLess(time.perf_counter() - start, 60)  # 측정 기준: 1분 이내 재처리
            self.assertEqual(second.repository.find_by_id(tx_id).status, S.COMPLETED)
            self.assertEqual(len(self.bank.executed_transfers(tx_id, "PAYOUT")), 1)
            # 재시작 후 구매자가 다시 눌러도 중복 지급 없음
            self.assertEqual(second.service.confirm_purchase(tx_id, BUYER).code, "DUPLICATE_REQUEST")
            return report
        finally:
            second.shutdown()

    def test_crash_before_bank_received_request(self):
        tx_id = self._crash_during_payout(crash_after_send=False)
        self.assertEqual(len(self.bank.executed_transfers(tx_id)), 0)
        report = self._restart_and_recover(tx_id)
        self.assertEqual(report.resent, 1)  # 은행 기록 없음(NOT_FOUND) → 같은 키로 재요청

    def test_crash_after_bank_processed_request(self):
        tx_id = self._crash_during_payout(crash_after_send=True)
        self.assertEqual(len(self.bank.executed_transfers(tx_id)), 1)
        report = self._restart_and_recover(tx_id)
        self.assertEqual(report.resent, 0)  # 결과 조회만으로 확정

    def test_job_runs_immediately_on_startup(self):
        tx_id = self._crash_during_payout(crash_after_send=True)
        second = new_system(db_path=self.db, bank_server=self.bank, job_interval_seconds=60)
        try:
            second.job.start()
            deadline = time.time() + 5
            while time.time() < deadline and second.repository.find_by_id(tx_id).status != S.COMPLETED:
                time.sleep(0.05)
            self.assertEqual(second.repository.find_by_id(tx_id).status, S.COMPLETED)
        finally:
            second.shutdown()


if __name__ == "__main__":
    unittest.main()
