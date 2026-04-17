"""
Run this ONCE locally to get a long-lived OAuth access token.
After running, copy TT_ACCESS_TOKEN and TT_REFRESH_TOKEN into Railway.
Railway never needs to deal with device challenges again.

Usage:
  python3 auth.py
"""

import requests
import json
import os
from dotenv import load_dotenv

load_dotenv()

BASE_URL      = os.getenv("TT_BASE_URL", "https://api.tastytrade.com")
USERNAME      = os.getenv("TT_USERNAME", "jondiorbeats")
PASSWORD      = os.getenv("TT_PASSWORD", "jonathan1123")
CLIENT_ID     = os.getenv("TT_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("TT_CLIENT_SECRET", "")
REDIRECT_URI  = os.getenv("TT_REDIRECT_URI", "https://tastytrade-bot-production.up.railway.app/callback")

def login_with_challenge():
    """Login with username/password, handle device challenge + 2FA if needed."""
    base_headers = {"Content-Type": "application/json"}

    print(f"\n1. Logging in as {USERNAME}...")
    r = requests.post(f"{BASE_URL}/sessions", json={
        "login": USERNAME, "password": PASSWORD, "remember-me": True,
    }, headers=base_headers)

    if r.status_code in (200, 201):
        print("✅ Logged in (no device challenge)")
        return r.json()["data"]["session-token"], base_headers

    if r.status_code != 403:
        raise Exception(f"Login failed: {r.status_code} {r.text}")

    err = r.json().get("error", {})
    if err.get("code") != "device_challenge_required":
        raise Exception(f"Login error: {r.text}")

    challenge_token = r.headers.get("X-Tastyworks-Challenge-Token", "")
    print(f"📱 Device challenge required — check your phone for an SMS code")
    code = input("   Enter code: ").strip()

    challenge_headers = {
        "Content-Type": "application/json",
        "X-Tastyworks-Challenge-Token": challenge_token,
    }

    r2 = requests.post(f"{BASE_URL}/device-challenge", json={"code": code},
                       headers=challenge_headers)
    if r2.status_code not in (200, 201):
        raise Exception(f"Device challenge failed: {r2.status_code} {r2.text}")
    print("✅ Device challenge accepted!")

    # Check if 2FA OTP also required
    r2_data = r2.json().get("data", {})
    redirect_headers = r2_data.get("redirect", {}).get("required-headers", [])
    if "X-Tastyworks-OTP" in str(redirect_headers):
        otp = input("   Enter 2FA code from your phone: ").strip()
        challenge_headers["X-Tastyworks-OTP"] = otp

    r3 = requests.post(f"{BASE_URL}/sessions", json={
        "login": USERNAME, "password": PASSWORD, "remember-me": True,
    }, headers=challenge_headers)

    if r3.status_code not in (200, 201):
        raise Exception(f"Final login failed: {r3.status_code} {r3.text}")

    print("✅ Logged in successfully!")
    return r3.json()["data"]["session-token"], challenge_headers


def get_oauth_token(session_token, session_headers):
    """Use the session to authorize OAuth app and get access+refresh tokens."""
    print("\n2. Authorizing OAuth app...")

    auth_headers = dict(session_headers)
    auth_headers["Authorization"] = session_token

    r = requests.post(f"{BASE_URL}/oauth/authorize", json={
        "client-id": CLIENT_ID,
        "redirect-uri": REDIRECT_URI,
        "response-type": "code",
        "scope": "read trade",
    }, headers=auth_headers)

    if r.status_code not in (200, 201):
        raise Exception(f"OAuth authorize failed: {r.status_code} {r.text}")

    # Extract the auth code from the redirect URL
    data = r.json()
    redirect_url = data.get("data", {}).get("redirect-uri", "")
    print(f"   Redirect: {redirect_url}")

    from urllib.parse import urlparse, parse_qs
    params = parse_qs(urlparse(redirect_url).query)
    code = params.get("code", [""])[0]

    if not code:
        raise Exception(f"No auth code in redirect: {redirect_url}")

    print(f"✅ Got auth code")

    # Exchange code for access token
    print("\n3. Exchanging code for access token...")
    r2 = requests.post(f"{BASE_URL}/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    })

    if r2.status_code not in (200, 201):
        raise Exception(f"Token exchange failed: {r2.status_code} {r2.text}")

    tokens = r2.json()
    access_token  = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")

    print("✅ Got OAuth access token!")

    with open("oauth_tokens.json", "w") as f:
        json.dump({"access_token": access_token, "refresh_token": refresh_token}, f, indent=2)

    print("\n" + "="*60)
    print("  Add these to Railway environment variables:")
    print("="*60)
    print(f"\n  TT_ACCESS_TOKEN={access_token}")
    print(f"  TT_REFRESH_TOKEN={refresh_token}\n")
    print("  (Access token expires — bot auto-refreshes using refresh token)")
    print("="*60)


if __name__ == "__main__":
    print("="*60)
    print("  Tastytrade OAuth Authorization")
    print("="*60)

    try:
        session_token, session_headers = login_with_challenge()
        get_oauth_token(session_token, session_headers)
    except Exception as e:
        print(f"\n❌ Error: {e}")
