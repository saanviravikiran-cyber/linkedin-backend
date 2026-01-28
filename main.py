# main.py
import os
import requests
from fastapi import FastAPI, Request, HTTPException
from pymongo import MongoClient
from datetime import datetime, timedelta
from uuid import uuid4
from cryptography.fernet import Fernet
from fastapi_utils.tasks import repeat_every

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
REDIRECT_URI = os.getenv(
    "LINKEDIN_REDIRECT_URI",
    "https://yourdomain.com/callback"
)

if not all([CLIENT_ID, CLIENT_SECRET, MONGO_URI, FERNET_KEY]):
    raise RuntimeError("Missing environment variables")

fernet = Fernet(FERNET_KEY)

# -------------------------------------------------
# MongoDB
# -------------------------------------------------
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["linkedin_agent"]
users = db["users"]

# -------------------------------------------------
# Utils
# -------------------------------------------------
def is_token_expired(user: dict) -> bool:
    expires_at = user.get("auth", {}).get("expires_at")
    if not expires_at:
        return True
    return datetime.utcnow() >= expires_at

# -------------------------------------------------
# Health
# -------------------------------------------------
@app.get("/")
def health():
    return {"status": "backend running"}

# -------------------------------------------------
# Create new internal user + generate OAuth link
# -------------------------------------------------
@app.post("/create_user")
def create_user(name: str):
    user_id = str(uuid4())
    users.insert_one({
        "internal_id": user_id,
        "name": name,
        "linkedin": {},
        "auth": {},
        "drafts": [],
        "posts": [],
        "has_posted": False,
        "created_at": datetime.utcnow()
    })

    # Generate LinkedIn OAuth URL
    oauth_url = (
        f"https://www.linkedin.com/oauth/v2/authorization?"
        f"response_type=code&client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope=r_liteprofile%20w_member_social"
        f"&state={user_id}"
    )

    return {"user_id": user_id, "oauth_url": oauth_url}

# -------------------------------------------------
# LinkedIn OAuth Callback
# -------------------------------------------------
@app.get("/callback")
def linkedin_callback(request: Request):
    code = request.query_params.get("code")
    state = request.query_params.get("state")  # internal user ID

    if not code or not state:
        raise HTTPException(status_code=400, detail="No code or state received")

    user = users.find_one({"internal_id": state})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

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
        raise HTTPException(status_code=400, detail=f"LinkedIn error: {token_res}")

    # Fetch LinkedIn profile
    profile = requests.get(
        "https://api.linkedin.com/v2/me",
        headers={"Authorization": f"Bearer {access_token}"},
    ).json()

    linkedin_user_id = profile.get("id")
    if not linkedin_user_id:
        raise HTTPException(status_code=400, detail="Could not fetch LinkedIn user")

    linkedin_urn = f"urn:li:person:{linkedin_user_id}"
    encrypted_token = fernet.encrypt(access_token.encode()).decode()

    # Update user with LinkedIn info
    users.update_one(
        {"internal_id": state},
        {
            "$set": {
                "linkedin.user_id": linkedin_user_id,
                "linkedin.urn": linkedin_urn,
                "auth.access_token": encrypted_token,
                "auth.expires_at": datetime.utcnow() + timedelta(seconds=expires_in),
                "updated_at": datetime.utcnow(),
            }
        }
    )

    return {"message": "OAuth success! You can now post automatically."}

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
    user = users.find_one({"linkedin.user_id": user_id}, {"_id": 0, "drafts": 1})
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
    if is_token_expired(user):
        raise HTTPException(status_code=401, detail="LinkedIn token expired.")

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

    if res.get("id"):
        users.update_one(
            {"linkedin.user_id": user_id},
            {"$push": {"posts": {"post_id": res["id"], "text": text, "posted_at": datetime.utcnow()}}}
        )

    return res

# -------------------------------------------------
# Background task: auto post welcome message for new users
# -------------------------------------------------
@app.on_event("startup")
@repeat_every(seconds=60)
def auto_post_for_new_users():
    new_users = users.find({
        "auth.access_token": {"$exists": True},
        "has_posted": False
    })

    for user in new_users:
        try:
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
                            "shareCommentary": {"text": "Welcome! First automated post."},
                            "shareMediaCategory": "NONE",
                        }
                    },
                    "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
                },
            ).json()

            if res.get("id"):
                users.update_one({"_id": user["_id"]}, {"$set": {"has_posted": True}})

        except Exception as e:
            print(f"Error auto-posting for user {user['_id']}: {e}")

