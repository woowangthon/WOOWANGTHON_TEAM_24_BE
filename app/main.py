from fastapi import FastAPI

app = FastAPI(title="보이는 강의 API")


@app.get("/")
def read_root():
    return {"status": "ok"}