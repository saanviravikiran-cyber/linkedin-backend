# main.py
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException
from pymongo import MongoClient
from fastapi import Request
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
pkce_states = db["pkce_states"]  # Store PKCE code_verifiers temporarily

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
# PKCE State Management
# -------------------------------------------------
@app.post("/pkce/store")
def store_pkce_state(state: str, code_verifier: str):
    """Store PKCE code_verifier temporarily, expires in 10 minutes"""
    pkce_states.insert_one({
        "state": state,
        "code_verifier": code_verifier,
        "created_at": datetime.utcnow(),
        "expires_at": datetime.utcnow() + timedelta(minutes=10)
    })
    return {"status": "stored"}

# -------------------------------------------------
# Manual post endpoint (used by agent tool)
# -------------------------------------------------
@app.post("/post")
def manual_post(user_id: str, text: str):
    user = users.find_one({"linkedin.user_id": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return create_linkedin_post(user, text)

# -------------------------------------------------
# OAuth callback endpoint
# -------------------------------------------------
@app.get("/callback")
def linkedin_callback(request: Request):
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")
    error_description = request.query_params.get("error_description")
    
    # Log incoming request
    print(f"=== OAuth Callback Received ===")
    print(f"Code (first 20 chars): {code[:20] if code else 'None'}...")
    print(f"State: {state}")
    print(f"Error: {error}")
    print(f"Error Description: {error_description}")
    
    # Check for OAuth errors
    if error:
        raise HTTPException(
            status_code=400, 
            detail=f"LinkedIn OAuth error: {error} - {error_description}"
        )
    
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")
    
    if not state:
        raise HTTPException(status_code=400, detail="Missing state parameter")
    
    # Retrieve code_verifier from database using state
    pkce_record = pkce_states.find_one({"state": state})
    
    if not pkce_record:
        raise HTTPException(
            status_code=400, 
            detail="PKCE state not found or expired. Please start the OAuth flow again."
        )
    
    # Check if expired
    if datetime.utcnow() > pkce_record.get("expires_at"):
        pkce_states.delete_one({"_id": pkce_record["_id"]})
        raise HTTPException(
            status_code=400,
            detail="PKCE state expired. Please start the OAuth flow again."
        )
    
    code_verifier = pkce_record["code_verifier"]
    print(f"Code Verifier retrieved: {code_verifier[:20]}...")
    
    # Delete the used PKCE state (one-time use)
    pkce_states.delete_one({"_id": pkce_record["_id"]})
    
    # 1. Exchange code for access token with PKCE
    print(f"=== Exchanging Code for Token (with PKCE) ===")
    print(f"CLIENT_ID: {CLIENT_ID}")
    print(f"REDIRECT_URI: {REDIRECT_URI}")
    print(f"CLIENT_SECRET: {'*' * 10} (hidden)")
    
    token_data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code_verifier": code_verifier,  # PKCE parameter
    }
    
    token_res = requests.post(
        "https://www.linkedin.com/oauth/v2/accessToken",
        data=token_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )

    print(f"Token response status: {token_res.status_code}")
    
    if token_res.status_code != 200:
        print(f"Token exchange failed!")
        print(f"Response: {token_res.text}")
        raise HTTPException(status_code=400, detail=token_res.text)

    token_data = token_res.json()
    access_token = token_data["access_token"]
    expires_in = token_data["expires_in"]

    # 2. Fetch LinkedIn profile using OpenID Connect userinfo
    print(f"=== Fetching LinkedIn Profile ===")
    profile_res = requests.get(
        "https://api.linkedin.com/v2/userinfo",
        headers={
            "Authorization": f"Bearer {access_token}",
        },
        timeout=10,
    )

    if profile_res.status_code != 200:
        print(f"Profile fetch failed: {profile_res.text}")
        raise HTTPException(status_code=400, detail="Failed to fetch LinkedIn profile")

    profile = profile_res.json()
    print(f"Profile response: {profile}")
    
    # OpenID returns 'sub' as the user identifier
    linkedin_id = profile["sub"]
    linkedin_urn = f"urn:li:person:{linkedin_id}"

    # 3. Encrypt token
    encrypted_token = fernet.encrypt(access_token.encode()).decode()

    # 4. Store / upsert user
    users.update_one(
        {"linkedin.user_id": linkedin_id},
        {
            "$set": {
                "linkedin": {
                    "user_id": linkedin_id,
                    "urn": linkedin_urn,
                },
                "auth": {
                    "access_token": encrypted_token,
                    "expires_at": datetime.utcnow() + timedelta(seconds=expires_in),
                },
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
        "message": "LinkedIn connected successfully",
        "linkedin_user_id": linkedin_id,
        "linkedin_urn": linkedin_urn,
        "expires_in": expires_in,
    }
