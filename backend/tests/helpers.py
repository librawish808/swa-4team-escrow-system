"""테스트 공통 도구: 거래를 원하는 단계까지 진행시키고, 서버 종료를 흉내 낸다."""
from __future__ import annotations

from app.bootstrap import EscrowSystem
from app.qas03_demo import CrashingBankPort, SimulatedCrash  # noqa: F401 (테스트에서 사용)
from app.transaction.models import Transaction

SELLER = "seller-1"
BUYER = "buyer-1"
AMOUNT = 100_000


def make_tx(system: EscrowSystem, until: str = "WAYBILL_REGISTERED") -> Transaction:
    """거래를 until 단계까지 정상 진행시킨다. 장애 주입은 이 다음에 한다."""
    s = system.service
    tx = s.create_transaction(SELLER, AMOUNT)
    if until == "WAITING_BUYER":
        return tx
    s.join_transaction(tx.id, BUYER, tx.invite_token)
    if until == "WAITING_DEPOSIT":
        return system.repository.find_by_id(tx.id)
    system.bank_server.make_deposit(tx.id, AMOUNT)
    assert s.confirm_deposit(tx.id).code == "DEPOSIT_CONFIRMED"
    if until == "DEPOSIT_CONFIRMED":
        return system.repository.find_by_id(tx.id)
    waybill = f"WB-{tx.id[:8]}"
    system.courier_server.add_shipment(waybill, recipient=BUYER)
    assert s.register_waybill(tx.id, SELLER, waybill).code == "WAYBILL_REGISTERED"
    return system.repository.find_by_id(tx.id)
