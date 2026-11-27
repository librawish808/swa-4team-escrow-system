"""QAS-02 operation claims. External APIs must run only after a successful claim."""

from typing import Literal

from app.database import connect_db
from app.external_ports import ExternalPort, ExternalResult


class TransactionConflict(Exception):
    """The state/version changed, the transaction is absent, or access is denied."""


def claim_operation(
    transaction_id: int,
    expected_version: int,
    actor_id: int,
    operation: Literal["WAYBILL", "REFUND"],
) -> dict:
    """Atomically authorize and claim an operation, committing before returning.

    actor_id must come from trusted authentication in the API layer, not a body
    field. A shared conflict result avoids exposing another user's transaction.
    This function does not call the bank/carrier or mark external work complete.
    """
    if min(transaction_id, expected_version, actor_id) < 1:
        raise ValueError("IDs and version must be positive")
    if operation not in ("WAYBILL", "REFUND"):
        raise ValueError("Unsupported operation")

    # SQL fragments below are fixed server constants, never user input.
    owner_column = "seller_id" if operation == "WAYBILL" else "buyer_id"
    next_status = "WAYBILL_PROCESSING" if operation == "WAYBILL" else "REFUND_PROCESSING"
    with connect_db() as db:
        row = db.execute(
            f"""
            UPDATE public.transactions
               SET status = %s, version = version + 1
             WHERE id = %s
               AND status = 'DEPOSIT_CONFIRMED'
               AND version = %s
               AND {owner_column} = %s
            RETURNING id, status, version, amount
            """,
            (next_status, transaction_id, expected_version, actor_id),
        ).fetchone()
        if row is None:
            raise TransactionConflict("Transaction cannot be claimed")
    # Connection context has committed successfully before granting permission.
    return row


def claim_waybill(transaction_id: int, expected_version: int, seller_id: int) -> dict:
    return claim_operation(transaction_id, expected_version, seller_id, "WAYBILL")


def claim_refund(transaction_id: int, expected_version: int, buyer_id: int) -> dict:
    return claim_operation(transaction_id, expected_version, buyer_id, "REFUND")


def _process(transaction_id, expected_version, actor_id, operation, port, waybill_no=None):
    claimed = claim_operation(transaction_id, expected_version, actor_id, operation)
    key = f"transaction:{transaction_id}:{claimed['version']}:{operation}"
    try:
        result = port.execute(
            operation=operation, transaction_id=transaction_id,
            amount=claimed["amount"], waybill_no=waybill_no, idempotency_key=key,
        )
    except Exception:
        # A timeout may happen after external success. Never release the claim or
        # automatically repeat the external operation. QAS-03 must reconcile it.
        result = ExternalResult.UNKNOWN
    if result != ExternalResult.SUCCESS:
        return {**claimed, "external_result": (
            result.value if isinstance(result, ExternalResult) else ExternalResult.UNKNOWN.value
        ), "recovery_required": True, "idempotency_key": key}

    final_status = "WAYBILL_REGISTERED" if operation == "WAYBILL" else "CANCELLED"
    # Preserve the exact claim version: a stale worker cannot finalize new work.
    # DB failures propagate; the committed processing claim remains for recovery.
    with connect_db() as db:
        final = db.execute(
            """UPDATE public.transactions
                  SET status = %s, version = version + 1, waybill_no = %s
                WHERE id = %s AND status = %s AND version = %s
                RETURNING id, status, version, amount""",
            (final_status, waybill_no, transaction_id, claimed["status"], claimed["version"]),
        ).fetchone()
        if final is None:
            raise TransactionConflict("Claim changed before finalization; recovery required")
    return {**final, "external_result": result.value,
            "recovery_required": False, "idempotency_key": key}


def register_waybill(transaction_id: int, expected_version: int, seller_id: int,
                     waybill_no: str, port: ExternalPort) -> dict:
    if not isinstance(waybill_no, str) or not waybill_no.strip():
        raise ValueError("Waybill number is required")
    return _process(transaction_id, expected_version, seller_id, "WAYBILL", port, waybill_no.strip())


def cancel_transaction(transaction_id: int, expected_version: int, buyer_id: int,
                       port: ExternalPort) -> dict:
    return _process(transaction_id, expected_version, buyer_id, "REFUND", port)
