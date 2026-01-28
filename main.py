from fastapi import FastAPI, Request

app = FastAPI()

@app.get("/")
def home():
    return {"status": "backend running"}

@app.get("/callback")
def callback(request: Request):
    return {
        "message": "callback hit",
        "query": dict(request.query_params)
    }
