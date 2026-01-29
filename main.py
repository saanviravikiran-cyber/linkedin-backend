# main.py
from fastapi import FastAPI, HTTPException
from pymongo import MongoClient
from datetime import datetime
from cryptography.fernet import Fernet
import requests
import os

# -------------------------------------------------
# Environment variables
# -------------------------------------------------
MONGO_URI = os.getenv("MONGO_URI")
CLIENT_ID = os.getenv("LINKEDIN_CLIENT_ID")
CLIENT_SECRET = os.getenv("LINKEDIN_CLIENT_SECRET")
FERNET_KEY = os.getenv("TOKEN_ENCRYPTION_KEY")
REDIRECT_URI = os.getenv("LINKEDIN_REDIRECT_URI")

if not all([MONGO_URI, CLIENT_ID, CLIENT_SECRET, FERNET_KEY, REDIRECT_URI]):
    raise RuntimeError("Missing required environment variables")

fernet = Fernet(FERNET_KEY)

# -------------------------------------------------
# MongoDB setup
# -------------------------------------------------
client = MongoClient(MONGO_URI)
db = client["linkedin_agent"]
users = db["users"]

# -------------------------------------------------
# FastAPI app
# -------------------------------------------------
app = FastAPI()

# -------------------------------------------------
# Helpers
# -------------------------------------------------
def is_token_expired(user: dict) -> bool:
    expires_at = user.get("auth", {}).get("expires_at")
    if not expires_at:
        return True
    return datetime.utcnow() >= expires_at


def get_access_token(user: dict) -> str:
    encrypted = user.get("auth", {}).get("access_token")
    if not encrypted:
        raise HTTPException(status_code=401, detail="Missing access token")
    return fernet.decrypt(encrypted.encode()).decode()


def create_linkedin_post(user: dict, text: str):
    if is_token_expired(user):
        raise HTTPException(
            status_code=401,
            detail="LinkedIn token expired. User must re-authenticate."
        )

    access_token = get_access_token(user)
    author_urn = user.get("linkedin", {}).get("urn")

    if not author_urn:
        raise HTTPException(status_code=400, detail="Missing LinkedIn URN")

    response = requests.post(
        "https://api.linkedin.com/v2/ugcPosts",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "X-Restli-Protocol-Version": "2.0.0",
        },
        json={
            "author": author_urn,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
            },
        },
        timeout=10,
    )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail=response.text
        )

    res = response.json()
    post_id = res.get("id")

    if post_id:
        users.update_one(
            {"_id": user["_id"]},
            {"$push": {
                "posts": {
                    "post_id": post_id,
                    "text": text,
                    "posted_at": datetime.utcnow(),
                }
            }},
        )

    return res

# -------------------------------------------------
# Health check
# -------------------------------------------------
@app.get("/")
def health():
    return {"status": "backend running"}

# -------------------------------------------------
# Manual post endpoint (used by agent tool)
# -------------------------------------------------
@app.post("/post")
def manual_post(user_id: str, text: str):
    user = users.find_one({"linkedin.user_id": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return create_linkedin_post(user, text)

