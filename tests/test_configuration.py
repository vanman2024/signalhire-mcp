#!/usr/bin/env python3
"""
SignalHire MCP Server - Configuration Test
Tests external callback server configuration
"""

import os
import sys
from pathlib import Path

# Load .env from server directory
from dotenv import load_dotenv

SERVER_DIR = Path(__file__).parent
ENV_FILE = SERVER_DIR / ".env"

if ENV_FILE.exists():
    load_dotenv(ENV_FILE)
    print(f"✅ Loaded .env from {ENV_FILE}")
else:
    print(f"❌ No .env file at {ENV_FILE}")
    sys.exit(1)

print("\n" + "="*60)
print("SignalHire MCP Server - Configuration Test")
print("="*60 + "\n")

# Test 1: API Key
print("1️⃣  Testing API Key Configuration...")
api_key = os.getenv("SIGNALHIRE_API_KEY")
if api_key and api_key != "your_signalhire_api_key_here":
    print(f"   ✅ API Key configured: {api_key[:10]}...")
else:
    print("   ❌ API Key not configured or using placeholder")
    sys.exit(1)

# Test 2: Callback URL Configuration
print("\n2️⃣  Testing Callback URL Configuration...")
external_callback = os.getenv("EXTERNAL_CALLBACK_URL")
callback_host = os.getenv("CALLBACK_SERVER_HOST", "0.0.0.0")
callback_port = os.getenv("CALLBACK_SERVER_PORT", "8000")

if external_callback and external_callback != "https://your-digitalocean-server.com/signalhire/callback":
    print(f"   ✅ External callback URL configured:")
    print(f"      {external_callback}")
    print(f"   ℹ️  Local callback server will NOT be started")
else:
    print(f"   ⚠️  External callback URL not set or using placeholder")
    print(f"   ℹ️  Will start local callback server at {callback_host}:{callback_port}")

# Test 3: Optional Services
print("\n3️⃣  Testing Optional Services...")
mem0_key = os.getenv("MEM0_API_KEY")
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_KEY")

if mem0_key:
    print(f"   ✅ Mem0 configured: {mem0_key[:10]}...")
else:
    print("   ℹ️  Mem0 not configured (optional)")

if supabase_url and supabase_key:
    print(f"   ✅ Supabase configured: {supabase_url}")
else:
    print("   ℹ️  Supabase not configured (optional)")

# Test 4: Import Test
print("\n4️⃣  Testing Server Imports...")
try:
    sys.path.insert(0, str(SERVER_DIR))
    from lib.signalhire_client import SignalHireClient
    from lib.callback_server import CallbackServer
    print("   ✅ All imports successful")
except ImportError as e:
    print(f"   ❌ Import error: {e}")
    sys.exit(1)

# Test 5: Callback URL Logic
print("\n5️⃣  Testing Callback URL Logic...")

def get_callback_url_logic():
    """Test the get_callback_url() logic"""
    external_url = os.getenv("EXTERNAL_CALLBACK_URL")
    if external_url:
        return external_url, "external"
    return f"http://{callback_host}:{callback_port}/signalhire/callback", "local"

url, source = get_callback_url_logic()
print(f"   ✅ Callback URL will be: {url}")
print(f"   ℹ️  Source: {source}")

# Final Summary
print("\n" + "="*60)
print("📊 Configuration Summary")
print("="*60)
print(f"API Key: {'✅ Configured' if api_key else '❌ Missing'}")
print(f"Callback: {'✅ External' if external_callback and 'your-' not in external_callback else '⚠️ Local'}")
print(f"Mem0: {'✅ Yes' if mem0_key else 'ℹ️ No'}")
print(f"Supabase: {'✅ Yes' if supabase_url else 'ℹ️ No'}")

if external_callback and "your-" in external_callback:
    print("\n⚠️  ACTION REQUIRED:")
    print("   Update EXTERNAL_CALLBACK_URL in .env with your actual DigitalOcean URL")
    print("   Example: https://callback.your-domain.com/signalhire/callback")

print("\n✅ Configuration test complete!")
