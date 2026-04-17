"""
Run this ONCE locally to authorize your Tastytrade account.
Handles the device challenge (SMS/email code), then saves a remember-token
that Railway uses so it never needs the challenge again.

Usage:
  python3 auth.py
"""

import requests
import json
import os
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("TT_BASE_URL", "https://api.tastytrade.com")
USERNAME = os.getenv("TT_USERNAME", "")
PASSWORD = os.getenv("TT_PASSWORD", "")
TOKENS_FILE = "tt_tokens.json"

def login_with_challenge():
    print("=" * 55)
    print("  Tastytrade Device Authorization")
    print("=" * 55)

    base_headers = {"Content-Type": "application/json"}

    # Step 1 — attempt login
    print(f"\n1. Logging in as {USERNAME}...")
    r = requests.post(f"{BASE_URL}/sessions", json={
        "login": USERNAME,
        "password": PASSWORD,
        "remember-me": True,
    }, headers=base_headers)

    if r.status_code in (200, 201):
        data = r.json()["data"]
        save_tokens(data)
        print_railway_instructions(data)
        return

    if r.status_code != 403:
        print(f"❌ Unexpected error: {r.status_code} {r.text}")
        return

    err = r.json().get("error", {})
    if err.get("code") != "device_challenge_required":
        print(f"❌ Login error: {r.text}")
        return

    # Extract challenge token from response headers
    challenge_token = r.headers.get("X-Tastyworks-Challenge-Token", "")
    print(f"\n✅ Got challenge token: {challenge_token[:30]}..." if challenge_token else "⚠️  No challenge token in headers")

    # Step 2 — enter the code sent via SMS/email
    print(f"\n   Enter the code Tastytrade sent to your phone/email:")
    code = input("   Code: ").strip()

    # Step 3 — submit challenge with token in headers
    print("\n2. Verifying code...")
    challenge_headers = {
        "Content-Type": "application/json",
        "X-Tastyworks-Challenge-Token": challenge_token,
    }
    r2 = requests.post(f"{BASE_URL}/device-challenge", json={
        "code": code,
    }, headers=challenge_headers)
    print(f"   Response: {r2.status_code} {r2.text[:200]}")

    if r2.status_code not in (200, 201):
        print(f"❌ Challenge failed")
        return

    print("✅ Code accepted!")

    # Step 4 — final login WITH challenge token + OTP
    print("\n3. Completing login...")
    otp = input("   Enter the 2FA code sent to your phone: ").strip()
    final_headers = {
        "Content-Type": "application/json",
        "X-Tastyworks-Challenge-Token": challenge_token,
        "X-Tastyworks-OTP": otp,
    }
    r3 = requests.post(f"{BASE_URL}/sessions", json={
        "login": USERNAME,
        "password": PASSWORD,
        "remember-me": True,
    }, headers=final_headers)

    print(f"   Response: {r3.status_code} {r3.text[:300]}")

    if r3.status_code not in (200, 201):
        print(f"❌ Final login failed: {r3.status_code}")
        return

    data = r3.json()["data"]
    save_tokens(data)
    print(f"\n✅ Fully authorized!")
    print_railway_instructions(data)


def save_tokens(data):
    tokens = {
        "session-token": data.get("session-token", ""),
        "remember-token": data.get("remember-token", ""),
    }
    with open(TOKENS_FILE, "w") as f:
        json.dump(tokens, f, indent=2)
    print(f"   Saved to {TOKENS_FILE}")


def print_railway_instructions(data):
    remember_token = data.get("remember-token", "")
    print("\n" + "=" * 55)
    print("  Add this to Railway environment variables:")
    print("=" * 55)
    print(f"\n  TT_REMEMBER_TOKEN={remember_token}\n")
    print("  (This token lasts ~90 days — re-run auth.py to refresh)")
    print("=" * 55)


if __name__ == "__main__":
    if not USERNAME or not PASSWORD:
        print("❌ Set TT_USERNAME and TT_PASSWORD in .env first")
    else:
        login_with_challenge()
