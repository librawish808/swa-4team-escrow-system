from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def root():
    return {"message": "Backend running"}

# QAS-03 가용성·장애 복구 데모 API (/api/qas-03/...)
from .qas03_api import router as qas03_router  # noqa: E402

app.include_router(qas03_router)
