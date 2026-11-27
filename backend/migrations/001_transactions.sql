-- Fresh database bootstrap for the QAS-02 prototype.
-- Already applied manually to the shared Supabase project: do not rerun there.
-- Buyer/seller IDs are temporary numeric IDs pending team authentication.
BEGIN;

CREATE TABLE public.transactions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    buyer_id BIGINT NOT NULL,
    seller_id BIGINT NOT NULL,
    amount BIGINT NOT NULL CHECK (amount > 0),
    status TEXT NOT NULL DEFAULT 'DEPOSIT_CONFIRMED'
        CHECK (status IN (
            'DEPOSIT_CONFIRMED',
            'WAYBILL_PROCESSING',
            'WAYBILL_REGISTERED',
            'REFUND_PROCESSING',
            'CANCELLED'
        )),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    waybill_no TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (buyer_id <> seller_id)
);

ALTER TABLE public.transactions ENABLE ROW LEVEL SECURITY;

COMMIT;
