"""Local demo only: uvicorn app.demo:app --host 127.0.0.1 --port 8000."""

from typing import Annotated, Literal

from fastapi import Depends, Header, HTTPException, Request

from app.database import connect_db
from app.external_ports import MockExternalPort
from app.main import create_app, external_port


def local_only(request: Request):
    if not request.client or request.client.host not in ("127.0.0.1", "::1", "testclient"):
        raise HTTPException(403, "Local demo only")


def demo_actor(
    actor: Annotated[Literal["10", "20"], Header(alias="X-Demo-Actor-Id")],
    _: Annotated[None, Depends(local_only)],
) -> int:
    return int(actor)


app = create_app(actor_dependency=demo_actor)
app.title = "QAS-02 로컬 데모 (은행·택배 Mock)"
app.description = "실제 인증이 없는 로컬 데모입니다. X-Demo-Actor-Id: 판매자 10 / 구매자 20. DB는 실제 테스트 프로젝트를 사용합니다."
port = MockExternalPort()
app.dependency_overrides[external_port] = lambda: port


@app.post("/demo/transactions", dependencies=[Depends(local_only)], status_code=201)
def new_demo_transaction():
    """입금 완료 가상 거래 생성 (구매자 20, 판매자 10, 금액 100000원)."""
    with connect_db() as db:
        row = db.execute(
            """INSERT INTO public.transactions (buyer_id, seller_id, amount)
               VALUES (20, 10, 100000) RETURNING id, status, version"""
        ).fetchone()
    return row
