# main.py
from fastapi import FastAPI, HTTPException
from fastapi_utils.tasks import repeat_every
from pymongo import MongoClient
from datetime import datetime, timedelta
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
REDIRECT_URI = "https://yourdomain.com/callback"  # Replace with your actual callback URL

if not all([MONGO_URI, CLIENT_ID, CLIENT_SECRET, FERNET_KEY]):
    raise RuntimeError("Missing required environment variables")

fernet = Fernet(FERNET_KEY)

# -------------------------------------------------
# MongoDB setup
# -------------------------------------------------
db = MongoClient(MONGO_URI)["linkedin_agent"]
users = db["users"]

# -------------------------------------------------
# FastAPI app
# -------------------------------------------------
app = FastAPI()

# -------------------------------------------------
# Helpers
# -------------------------------------------------
def is_token_expired(user: dict) -> bool:
    return datetime.utcnow() >= user["auth"]["expires_at"]

def refresh_token(user: dict):
    """
    Refresh the LinkedIn token if expired.
    LinkedIn might not provide refresh tokens; for long-lived tokens,
    this could be a re-auth flow. Here we just raise if expired.
    """
    if is_token_expired(user):
        raise HTTPException(status_code=401, detail=f"Token expired for user {user['linkedin']['user_id']}")

def create_linkedin_post(user: dict, text: str):
    access_token = fernet.decrypt(user["auth"]["access_token"].encode()).decode()
    author_urn = user["linkedin"]["urn"]

    res = requests.post(
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
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        },
    ).json()

    post_id = res.get("id")
    if post_id:
        users.update_one(
            {"_id": user["_id"]},
            {"$push": {"posts": {"post_id": post_id, "text": text, "posted_at": datetime.utcnow()}}},
        )
    return res

# -------------------------------------------------
# Health check
# -------------------------------------------------
@app.get("/")
def health():
    return {"status": "backend running"}

# -------------------------------------------------
# Automatic onboarding task
# -------------------------------------------------
@app.on_event("startup")
@repeat_every(seconds=60)  # runs every minute, adjust as needed
def auto_post_for_new_users():
    new_users = users.find({"has_posted": {"$ne": True}})
    for user in new_users:
        try:
            if is_token_expired(user):
                refresh_token(user)  # This will raise if token expired

            create_linkedin_post(user, "Welcome to LinkedIn! Your backend is now connected.")
            users.update_one({"_id": user["_id"]}, {"$set": {"has_posted": True}})
            print(f"Posted welcome for user {user['linkedin']['user_id']}")
        except HTTPException as e:
            print(f"Skipping user {user['linkedin']['user_id']}: {e.detail}")

# -------------------------------------------------
# Optional: Manual post endpoint
# -------------------------------------------------
@app.post("/post")
def manual_post(user_id: str, text: str):
    user = users.find_one({"linkedin.user_id": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if is_token_expired(user):
        refresh_token(user)
    return create_linkedin_post(user, text)

