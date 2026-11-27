"""Relational & cross-system invariant checker for escrow consistency verification."""

from collections import defaultdict
from dataclasses import dataclass

from .domain import (
    Deposit,
    DepositAllocation,
    OperationStatus,
    ReceiptStatus,
    instant,
)
from .policy import DeadlinePolicy
from .repository import SqlTransactionRepository


@dataclass(frozen=True)
class InvariantReport:
    passed: bool
    violations: tuple[str, ...]
    checked_transactions: int
    checked_operations: int
    checked_transfers: int


def verify_money_conservation(
    deposit: Deposit, allocations: list[DepositAllocation]
) -> list[str]:
    """Conservation of money: received deposit amount must equal sum of allocations."""
    violations: list[str] = []
    if deposit.received_amount <= 0:
        violations.append(
            f"Deposit {deposit.bank_deposit_id} has non-positive amount {deposit.received_amount}"
        )
    for alloc in allocations:
        if alloc.amount <= 0:
            violations.append(
                f"Allocation {alloc.kind} for {deposit.bank_deposit_id} has non-positive amount {alloc.amount}"
            )
    total_allocated = sum(a.amount for a in allocations)
    if total_allocated != deposit.received_amount:
        violations.append(
            f"Money conservation violated for {deposit.bank_deposit_id}: "
            f"received={deposit.received_amount} != allocated={total_allocated}"
        )
    return violations


class InvariantChecker:
    """Verifies relational, causal, and DB<->bank ledger invariants with zero tolerance."""

    @staticmethod
    def verify_database(
        repository: SqlTransactionRepository, *, strict_reconciliation: bool = True
    ) -> InvariantReport:
        violations: list[str] = []
        with repository.connection() as db:
            transactions = {
                row["id"]: dict(row)
                for row in db.execute("SELECT * FROM transactions").fetchall()
            }
            receipts_by_tx: dict[str, list[dict]] = defaultdict(list)
            for row in db.execute(
                "SELECT * FROM receipts ORDER BY received_at, rowid"
            ).fetchall():
                receipts_by_tx[row["transaction_id"]].append(dict(row))

            operations = [
                dict(row) for row in db.execute("SELECT * FROM operations").fetchall()
            ]
            ops_by_tx: dict[str, list[dict]] = defaultdict(list)
            ops_by_key: dict[str, dict] = {}
            for op in operations:
                ops_by_tx[op["transaction_id"]].append(op)
                ops_by_key[op["request_key"]] = op

            transfers = {
                row["request_key"]: dict(row)
                for row in db.execute("SELECT * FROM mock_bank_transfers").fetchall()
            }
            events_by_tx: dict[str, list[int]] = defaultdict(list)
            for row in db.execute(
                "SELECT transaction_id, sequence FROM events ORDER BY sequence"
            ).fetchall():
                events_by_tx[row["transaction_id"]].append(row["sequence"])

        # 1. Schema, deadline, version, and event sequence monotonicity
        for tx_id, tx in transactions.items():
            if tx["amount"] <= 0:
                violations.append(f"Transaction {tx_id} has non-positive amount")
            if tx["version"] < 0:
                violations.append(f"Transaction {tx_id} has negative version")
            expected_deadline = DeadlinePolicy.deadline_of(
                instant(tx["return_notice_published_at"])
            )
            if instant(tx["deadline"]) != expected_deadline:
                violations.append(
                    f"Transaction {tx_id} deadline mismatch: {tx['deadline']} != {expected_deadline.isoformat()}"
                )
            seqs = events_by_tx[tx_id]
            if any(seqs[i] >= seqs[i + 1] for i in range(len(seqs) - 1)):
                violations.append(
                    f"Transaction {tx_id} events sequence is not strictly monotonic"
                )

        # 2. Mutual exclusion & 3. Causal integrity
        for tx_id, tx_ops in ops_by_tx.items():
            if len(tx_ops) > 1:
                violations.append(
                    f"Mutual exclusion violated for {tx_id}: {len(tx_ops)} principal operations"
                )
            tx = transactions.get(tx_id)
            if tx is None:
                violations.append(f"Orphan operation for missing transaction {tx_id}")
                continue
            deadline = instant(tx["deadline"])
            timely = [
                r
                for r in receipts_by_tx[tx_id]
                if DeadlinePolicy.is_before_deadline(instant(r["received_at"]), deadline)
            ]
            for op in tx_ops:
                if op["amount"] != tx["amount"]:
                    violations.append(
                        f"Amount mismatch for {tx_id}: op={op['amount']} != tx={tx['amount']}"
                    )
                if op["kind"] == "REFUND":
                    if not any(r["status"] == ReceiptStatus.DELIVERED for r in timely):
                        violations.append(
                            f"Causal violation for {tx_id}: REFUND without timely DELIVERED receipt"
                        )
                elif op["kind"] == "PAYOUT":
                    if any(r["status"] != ReceiptStatus.INVALID for r in timely):
                        violations.append(
                            f"Causal violation for {tx_id}: PAYOUT despite timely non-INVALID receipt"
                        )

        # 4. Cross-system reconciliation (DB operations <-> mock bank ledger)
        for key, op in ops_by_key.items():
            if op["status"] == OperationStatus.SUCCEEDED:
                transfer = transfers.get(key)
                if transfer is None:
                    violations.append(
                        f"Reconciliation violation: operation {key} is SUCCEEDED in DB but missing in bank ledger"
                    )
                else:
                    for field in ("transaction_id", "kind", "recipient_id", "amount"):
                        if transfer[field] != op[field]:
                            violations.append(
                                f"Reconciliation mismatch on {key}.{field}: db={op[field]} != bank={transfer[field]}"
                            )

        for key, transfer in transfers.items():
            op = ops_by_key.get(key)
            if op is None:
                violations.append(
                    f"Reconciliation violation: bank transfer {key} has no DB operation record"
                )
                continue
            for field in ("transaction_id", "kind", "recipient_id", "amount"):
                if transfer[field] != op[field]:
                    violations.append(
                        f"Reconciliation mismatch on {key}.{field}: bank={transfer[field]} != db={op[field]}"
                    )
            if strict_reconciliation and op["status"] != OperationStatus.SUCCEEDED:
                violations.append(
                    f"Reconciliation violation: bank transfer {key} succeeded but DB status is {op['status']}"
                )

        return InvariantReport(
            passed=len(violations) == 0,
            violations=tuple(violations),
            checked_transactions=len(transactions),
            checked_operations=len(operations),
            checked_transfers=len(transfers),
        )
