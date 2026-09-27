import uvicorn
from fastapi import FastAPI

from billing.api import router as billing_router

app = FastAPI(title="usage-metering-billing")
app.include_router(billing_router, prefix="/billing")


@app.get("/")
def read_root():
    return {"Hello": "World"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
