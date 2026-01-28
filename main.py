from fastapi import FastAPI, Request
import os
import requests

app = FastAPI()

CLIENT_ID = os.getenv("LINKEDIN_CLIENT_ID")
CLIENT_SECRET = os.getenv("LINKEDIN_CLIENT_SECRET")
REDIRECT_URI = "https://linkedin-backend-production-1a8f.up.railway.app/callback"


@app.get("/")
def home():
    return {"status": "backend running"}


@app.get("/callback")
def callback(request: Request):
    code = request.query_params.get("code")

    if not code:
        return {"error": "No code received"}

    token_res = requests.post(
        "https://www.linkedin.com/oauth/v2/accessToken",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    ).json()

    access_token = token_res.get("access_token")

    if not access_token:
        return token_res

    me_res = requests.get(
        "https://api.linkedin.com/v2/me",
        headers={
            "Authorization": f"Bearer {access_token}"
        },
    ).json()

    return {
        "access_token": access_token,
        "linkedin_id": me_res.get("id"),
        "linkedin_urn": f"urn:li:person:{me_res.get('id')}"
    }


