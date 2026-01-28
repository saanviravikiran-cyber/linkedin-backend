from fastapi import FastAPI, Request, HTTPException
import os
import requests
from pymongo import MongoClient
from datetime import datetime, timedelta
from uuid import uuid4
from cryptography.fernet import Fernet

# -------------------------------------------------
# App
# -------------------------------------------------
app = FastAPI()

# -------------------------------------------------
# Environment
# -------------------------------------------------
CLIENT_ID = os.getenv("LINKEDIN_CLIENT_ID")
CLIENT_SECRET = os.getenv("LINKEDIN_CLIENT_SECRET")
MONGO_URI = os.getenv("MONGO_URI")
FERNET_KEY = os.getenv("TOKEN_ENCRYPTION_KEY")

REDIRECT_URI = "https://linkedin-backend-production-1a8f.up.railway.app/callback"

if not all([CLIENT_ID, CLIENT_SECRET, MONGO_URI, FERNET_KEY]):
    raise RuntimeError("Missing required environment variables")

fernet = Fernet(FERNET_KEY)

# -------------------------------------------------
# MongoDB
# -------------------------------------------------
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["linkedin_agent"]
users = db["users"]

# -------------------------------------------------
# Health
# -------------------------------------------------
@app.get("/")
def health():
    return {"status": "backend running"}

# -------------------------------------------------
# LinkedIn OAuth Callback
# -------------------------------------------------
@app.get("/callback")
def linkedin_callback(request: Request):
    code = request.query_params.get("code")

    if not code:
        raise HTTPException(status_code=400, detail="No code received")

    # Exchange code -> access token
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
    expires_in = token_res.get("expires_in")

    if not access_token:
        return token_res

    # Fetch user info (OpenID)
    userinfo = requests.get(
        "https://api.linkedin.com/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
    ).json()

    linkedin_user_id = userinfo.get("sub")

    if not linkedin_user_id:
        raise HTTPException(status_code=400, detail="Unable to fetch LinkedIn user")

    linkedin_urn = f"urn:li:person:{linkedin_user_id}"

    encrypted_token = fernet.encrypt(access_token.encode()).decode()

    # Upsert user
    users.update_one(
        {"linkedin.user_id": linkedin_user_id},
        {
            "$set": {
                "linkedin.user_id": linkedin_user_id,
                "linkedin.urn": linkedin_urn,
                "auth.access_token": encrypted_token,
                "auth.expires_at": datetime.utcnow()
                + timedelta(seconds=expires_in),
                "updated_at": datetime.utcnow(),
            },
            "$setOnInsert": {
                "drafts": [],
                "posts": [],
                "created_at": datetime.utcnow(),
            },
        },
        upsert=True,
    )

    return {
        "linkedin_user_id": linkedin_user_id,
        "linkedin_urn": linkedin_urn,
        "expires_in": expires_in,
    }

# -------------------------------------------------
# Drafts
# -------------------------------------------------
@app.post("/drafts")
def add_draft(user_id: str, text: str):
    draft = {
        "id": str(uuid4()),
        "text": text,
        "created_at": datetime.utcnow(),
    }

    result = users.update_one(
        {"linkedin.user_id": user_id},
        {"$push": {"drafts": draft}},
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")

    return {"status": "draft added", "draft": draft}


@app.get("/drafts")
def get_drafts(user_id: str):
    user = users.find_one(
        {"linkedin.user_id": user_id},
        {"_id": 0, "drafts": 1},
    )

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return user["drafts"]

# -------------------------------------------------
# Create LinkedIn Post
# -------------------------------------------------
@app.post("/post")
def create_post(user_id: str, text: str):
    user = users.find_one({"linkedin.user_id": user_id})

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    encrypted_token = user["auth"]["access_token"]
    access_token = fernet.decrypt(encrypted_token.encode()).decode()
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
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
            },
        },
    ).json()

    post_id = res.get("id")

    if post_id:
        users.update_one(
            {"linkedin.user_id": user_id},
            {
                "$push": {
                    "posts": {
                        "post_id": post_id,
                        "text": text,
                        "posted_at": datetime.utcnow(),
                    }
                }
            },
        )

    return res

