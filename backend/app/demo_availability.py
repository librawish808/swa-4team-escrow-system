"""가용성·장애 복구성 QAS 시연 (발표·실행 화면용).

실행 (backend 폴더에서):  python -m app.demo_availability
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile

from .bootstrap import build_system
from .external.bank_adapter import BankAdapter
from .external.bank_client import BankApiClient
from .external.mock_bank import BankFault, MockBankServer

SELLER, BUYER, AMOUNT = "seller-1", "buyer-1", 100_000


def title(text: str) -> None:
    print(f"\n{'=' * 64}\n{text}\n{'=' * 64}")


def step(text: str) -> None:
    print(f"  - {text}")


def ready_tx(system):
    s = system.service
    tx = s.create_transaction(SELLER, AMOUNT)
    s.join_transaction(tx.id, BUYER, tx.invite_token)
    system.bank_server.make_deposit(tx.id, AMOUNT)
    s.confirm_deposit(tx.id)
    system.courier_server.add_shipment(f"WB-{tx.id[:6]}", recipient=BUYER)
    s.register_waybill(tx.id, SELLER, f"WB-{tx.id[:6]}")
    return tx


def scenario_external() -> None:
    title("QAS-AV-01  외부 장애: 지급 요청 중 은행 응답 유실")
    system = build_system(timeout_millis=300)
    bank = system.bank_server
    tx = ready_tx(system)
    step(f"거래 {tx.id[:8]} 운송장 등록 완료, 금액 {AMOUNT:,}원")

    bank.set_fault(BankFault.RESPONSE_LOST)
    step("[장애 주입] 은행이 지급은 처리하지만 응답이 타임아웃(300ms) 뒤에 도착")
    r = system.service.confirm_purchase(tx.id, BUYER)
    step(f"구매확정 결과: {r.code} / 상태 {r.status.value}  ← 완료로 표시하지 않음")
    step(f"은행 실제 지급 건수: {len(bank.executed_transfers(tx.id))}건")

    r = system.service.confirm_purchase(tx.id, BUYER)
    step(f"구매자 재요청: {r.code}  ← 중복 지급 방지")

    other = system.service.create_transaction(SELLER, 50_000)
    step(f"장애 중 다른 기능(거래 등록): 정상 → 거래 {other.id[:8]} 생성")

    bank.set_fault(BankFault.NONE)
    step("[복구] 은행 정상화 → ExpirationJob 실행")
    report = system.job.run()
    final = system.repository.find_by_id(tx.id)
    step(f"복구 결과: 확인 {report.checked}건, 확정 {report.completed}건, 재요청 {report.resent}건")
    step(f"최종 상태: {final.status.value} / 은행 지급 {len(bank.executed_transfers(tx.id))}건")
    system.shutdown()


def scenario_internal() -> None:
    title("QAS-AV-02  내부 장애: 은행 호출 직전 서버 비정상 종료")
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    bank = MockBankServer()

    class CrashBeforeSend(BankAdapter):
        def request_payout(self, transaction_id, request_key):
            raise SystemExit("서버 비정상 종료")

    first = build_system(db_path=db, bank_server=bank, bank_port=CrashBeforeSend(BankApiClient(bank, 300)))
    tx = ready_tx(first)
    step(f"거래 {tx.id[:8]} 구매확정 요청")
    try:
        first.service.confirm_purchase(tx.id, BUYER)
    except SystemExit as e:
        step(f"[장애] {e}  (상태는 호출 전에 DB에 저장됨)")
    first.shutdown()

    second = build_system(db_path=db, bank_server=bank)
    step(f"서버 재시작 → DB의 거래 상태: {second.repository.find_by_id(tx.id).status.value}")
    report = second.job.run()
    final = second.repository.find_by_id(tx.id)
    step(f"재시작 직후 복구: 은행 기록 없음 → 같은 requestKey로 재요청 {report.resent}건")
    step(f"최종 상태: {final.status.value} / 은행 지급 {len(bank.executed_transfers(tx.id))}건")
    second.shutdown()
    os.remove(db)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.ERROR)
    scenario_external()
    scenario_internal()
    print()


if __name__ == "__main__":
    main()
