# main.py
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Depends, Header
from pymongo import MongoClient
from fastapi import Request
from cryptography.fernet import Fernet
import requests
import os
from typing import Optional

# -------------------------------------------------
# Environment variables
# -------------------------------------------------
MONGO_URI = os.getenv("MONGO_URI")
COMPOSIO_AUTH_CONFIG_ID = os.getenv("COMPOSIO_AUTH_CONFIG_ID", "ac_TeOqCrPUelSx")
COMPOSIO_API_KEY = os.getenv("COMPOSIO_API_KEY")  # Add this to Railway
FERNET_KEY = os.getenv("TOKEN_ENCRYPTION_KEY")
AGENT_TOKEN = os.getenv("AGENT_TOKEN", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJkZXZfNmUyZjk0OWYtNWVkNi00MTA4LTgzYTItZTE0ZmNkMGEyZGZiIiwiZXhwIjoxNzY5ODU3MzY2fQ.DpcNPbwGljxWs7yTbTj0RAaOXj8JOVdXKWbx9oSMff8")

if not all([MONGO_URI, COMPOSIO_API_KEY, FERNET_KEY]):
    raise RuntimeError("Missing required environment variables")

fernet = Fernet(FERNET_KEY)

# -------------------------------------------------
# Authentication
# -------------------------------------------------
def verify_agent_token(authorization: Optional[str] = Header(None)):
    """Verify the agent token from Authorization header"""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authorization header")
    
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header format")
    
    token = authorization.replace("Bearer ", "")
    
    if token != AGENT_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid agent token")
    
    return True

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
# Composio Integration
# -------------------------------------------------
def get_composio_connection(entity_id: str):
    """Get LinkedIn connection from Composio for a specific entity"""
    try:
        response = requests.get(
            f"https://backend.composio.dev/api/v1/connectedAccounts",
            headers={
                "X-API-Key": COMPOSIO_API_KEY,
            },
            params={
                "user_uuid": entity_id,
                "appName": "linkedin"
            },
            timeout=10
        )
        
        if response.status_code == 200:
            connections = response.json().get("items", [])
            if connections:
                return connections[0]  # Return first LinkedIn connection
        return None
    except Exception as e:
        print(f"Error fetching Composio connection: {e}")
        return None

def execute_composio_action(entity_id: str, action: str, input_data: dict):
    """Execute a Composio action for LinkedIn"""
    try:
        response = requests.post(
            f"https://backend.composio.dev/api/v2/actions/{action}/execute",
            headers={
                "X-API-Key": COMPOSIO_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "entityId": entity_id,
                "input": input_data
            },
            timeout=30
        )
        
        if response.status_code == 200:
            return response.json()
        else:
            print(f"Composio action failed: {response.text}")
            return None
    except Exception as e:
        print(f"Error executing Composio action: {e}")
        return None

# -------------------------------------------------
# Health check
# -------------------------------------------------
@app.get("/")
def health():
    return {"status": "backend running", "composio_enabled": True}

# -------------------------------------------------
# Composio Auth Endpoint
# -------------------------------------------------
@app.get("/auth/composio")
def get_composio_auth_url(entity_id: str):
    """Get Composio authentication URL for LinkedIn"""
    try:
        response = requests.post(
            "https://backend.composio.dev/api/v1/connectedAccounts",
            headers={
                "X-API-Key": COMPOSIO_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "integrationId": COMPOSIO_AUTH_CONFIG_ID,
                "userUuid": entity_id,
                "redirectUrl": "https://linkedin-backend-production-1a8f.up.railway.app/auth/callback"
            },
            timeout=10
        )
        
        if response.status_code == 200:
            data = response.json()
            return {
                "auth_url": data.get("redirectUrl"),
                "connection_id": data.get("connectionId"),
                "entity_id": entity_id
            }
        else:
            raise HTTPException(status_code=400, detail=response.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# -------------------------------------------------
# Check connection status
# -------------------------------------------------
@app.get("/auth/status/{entity_id}")
def check_auth_status(entity_id: str):
    """Check if entity has active LinkedIn connection"""
    connection = get_composio_connection(entity_id)
    
    if connection:
        return {
            "connected": True,
            "entity_id": entity_id,
            "connection_id": connection.get("id"),
            "status": connection.get("status"),
            "app_name": connection.get("appName")
        }
    else:
        return {
            "connected": False,
            "entity_id": entity_id,
            "message": "No LinkedIn connection found"
        }

# -------------------------------------------------
# Manual post endpoint (used by agent tool) - PROTECTED
# -------------------------------------------------
@app.post("/post")
def manual_post(entity_id: str, text: str, authenticated: bool = Depends(verify_agent_token)):
    """Post to LinkedIn via Composio - requires valid agent token"""
    print(f"Posting for entity: {entity_id}")
    
    # Check if user has Composio connection
    connection = get_composio_connection(entity_id)
    if not connection:
        raise HTTPException(
            status_code=404, 
            detail="LinkedIn not connected via Composio. Please authenticate first."
        )
    
    # Execute LinkedIn post action via Composio
    result = execute_composio_action(
        entity_id=entity_id,
        action="LINKEDIN_CREATE_POST",
        input_data={
            "text": text,
            "visibility": "PUBLIC"
        }
    )
    
    if not result:
        raise HTTPException(status_code=500, detail="Failed to post to LinkedIn")
    
    # Store post in MongoDB
    users.update_one(
        {"composio_entity_id": entity_id},
        {
            "$push": {
                "posts": {
                    "post_id": result.get("data", {}).get("id"),
                    "text": text,
                    "posted_at": datetime.utcnow(),
                    "composio_result": result
                }
            },
            "$set": {
                "updated_at": datetime.utcnow()
            },
            "$setOnInsert": {
                "created_at": datetime.utcnow(),
                "drafts": []
            }
        },
        upsert=True
    )
    
    return {
        "success": True,
        "message": "Posted to LinkedIn successfully",
        "result": result
    }

# -------------------------------------------------
# PKCE State Management (kept for backward compatibility)
# -------------------------------------------------
@app.post("/pkce/store")
def store_pkce_state(state: str, code_verifier: str, code_challenge: str = None):
    """Legacy endpoint - not used with Composio"""
    return {"status": "deprecated", "message": "Use Composio auth instead"}

# -------------------------------------------------
# Draft management
# -------------------------------------------------
@app.post("/drafts")
def save_draft(entity_id: str, content: str, tags: list = []):
    """Save a draft post"""
    draft_id = f"draft_{datetime.utcnow().timestamp()}"
    
    users.update_one(
        {"composio_entity_id": entity_id},
        {
            "$push": {
                "drafts": {
                    "draft_id": draft_id,
                    "content": content,
                    "tags": tags,
                    "created_at": datetime.utcnow()
                }
            },
            "$setOnInsert": {
                "created_at": datetime.utcnow(),
                "posts": []
            }
        },
        upsert=True
    )
    
    return {"draft_id": draft_id, "status": "saved"}
