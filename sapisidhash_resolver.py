"""
Google Flow Cookie-Based Authentication Resolver.

Uses SAPISIDHASH + browser fingerprint headers to authenticate with
aisandbox-pa.googleapis.com REST API endpoints using raw cookies.

NO ya29 Bearer token needed. The SAPISIDHASH approach signs requests
with the SAPISID cookie + a SHA-1 HMAC, combined with browser-fingerprint
headers to pass Google's WAF.

Key insight from gflow_cli source:
- Origin must be "https://labs.google" (NOT flow.google.com)
- Browser fingerprint headers (sec-ch-ua, sec-fetch-*, etc.) are REQUIRED
- httpx (not curl_cffi) should be used to avoid TLS fingerprint conflicts
"""

import hashlib
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple


def read_api_key():
    """Read the Flow API key."""
    key_file = "/tmp/flow_api_key.txt"
    if os.path.exists(key_file):
        with open(key_file) as f:
            return f.read().strip()
    try:
        sys.path.insert(0, "/root/af_project/flow-agent/flow-agent")
        from flow_engine.config import API_KEY
        return API_KEY
    except Exception:
        pass
    return None


API_KEY = read_api_key()
API_BASE = "https://aisandbox-pa.googleapis.com"
SAPISIDHASH_ORIGIN = "https://labs.google"

# Essential cookies for Google session auth
ESSENTIAL_COOKIES = [
    "SID", "HSID", "SSID", "APISID", "SAPISID",
    "__Secure-1PSID", "__Secure-3PSID",
]

OPTIONAL_COOKIES = [
    "__Secure-1PAPISID", "__Secure-3PAPISID",
    "SIDCC", "__Secure-1PSIDCC", "__Secure-3PSIDCC",
    "OSID", "__Secure-OSID",
    "NID", "AEC", "SOCS",
    "__Secure-1PSIDTS", "__Secure-3PSIDTS",
]

# Browser fingerprint headers that Google's WAF requires
# (from gflow_cli _fingerprint.py REQUIRED_HEADERS)
BROWSER_FINGERPRINT_HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Google Chrome";v="137", "Chromium";v="137", "Not/A)Brand";v="24"',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-mobile": "?0",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "cross-site",
    "origin": SAPISIDHASH_ORIGIN,
    "referer": f"{SAPISIDHASH_ORIGIN}/",
}


# ─── Cookie Parsing ────────────────────────────────────────────

def parse_devtools_cookies_tab(raw_text: str) -> dict:
    """Parse cookies from Chrome DevTools table copy (tab-separated)."""
    cookies = {}
    for line in raw_text.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" in line:
            parts = line.split("\t")
            if len(parts) >= 2:
                name, value = parts[0].strip(), parts[1].strip()
                domain = parts[2].strip() if len(parts) > 2 and parts[2].strip() else ".google.com"
                if name and value:
                    cookies[name] = {"name": name, "value": value, "domain": domain}
        elif line.lower().startswith("cookie:"):
            for pair in line.split(":", 1)[1].strip().split(";"):
                pair = pair.strip()
                if "=" in pair:
                    name, value = pair.split("=", 1)
                    name, value = name.strip(), value.strip()
                    if name and value:
                        cookies[name] = {"name": name, "value": value, "domain": ".google.com"}
        elif "=" in line:
            name, value = line.split("=", 1)
            name, value = name.strip(), value.strip()
            if name and value:
                cookies[name] = {"name": name, "value": value, "domain": ".google.com"}
    return cookies


def parse_json_cookies(json_text: str) -> dict:
    """Parse cookies from JSON array format."""
    cookies = {}
    try:
        data = json.loads(json_text)
        if isinstance(data, list):
            for c in data:
                name = c.get("name", "")
                value = c.get("value", "")
                domain = c.get("domain", ".google.com")
                if name and value:
                    cookies[name] = {"name": name, "value": value, "domain": domain}
    except json.JSONDecodeError:
        pass
    return cookies


def parse_cookies_auto(raw_text: str) -> dict:
    """Auto-detect cookie format and parse."""
    raw_text = raw_text.strip()
    if raw_text.startswith("[") or raw_text.startswith("{"):
        result = parse_json_cookies(raw_text)
        if result:
            return result
    return parse_devtools_cookies_tab(raw_text)


def validate_cookies(cookies: dict) -> dict:
    """Check which essential cookies are present."""
    present = [n for n in ESSENTIAL_COOKIES if n in cookies and cookies[n].get("value")]
    missing = [n for n in ESSENTIAL_COOKIES if n not in present]
    extra = [n for n in OPTIONAL_COOKIES if n in cookies and cookies[n].get("value")]
    return {
        "valid": "SAPISID" in present and "SID" in present,
        "essential_present": present,
        "essential_missing": missing,
        "extra_present": extra,
        "total_cookies": len(cookies),
    }


def build_cookie_header(cookies: dict) -> str:
    """Build Cookie header string from parsed cookies dict."""
    return "; ".join(f"{name}={info['value']}" for name, info in cookies.items())


# ─── SAPISIDHASH ────────────────────────────────────────────────

def generate_sapisidhash(sapisid: str, origin: str = SAPISIDHASH_ORIGIN) -> str:
    """Generate SAPISIDHASH authorization header value.
    
    Format: SAPISIDHASH <timestamp>_<sha1(timestamp + " " + sapisid + " " + origin)>
    """
    timestamp = int(time.time())
    hash_input = f"{timestamp} {sapisid} {origin}"
    hash_value = hashlib.sha1(hash_input.encode("utf-8")).hexdigest()
    return f"SAPISIDHASH {timestamp}_{hash_value}"


def build_sapisidhash_headers(cookies: dict) -> dict:
    """Build full headers for SAPISIDHASH auth against aisandbox-pa API.
    
    Combines:
    - SAPISIDHASH Authorization header
    - Browser fingerprint headers (required by Google WAF)
    - Cookie header with all session cookies
    """
    sapisid = cookies.get("SAPISID", {}).get("value")
    if not sapisid:
        raise ValueError("SAPISID cookie not found")
    
    headers = dict(BROWSER_FINGERPRINT_HEADERS)
    headers["authorization"] = generate_sapisidhash(sapisid)
    headers["cookie"] = build_cookie_header(cookies)
    headers["content-type"] = "text/plain;charset=UTF-8"
    headers["x-goog-authuser"] = "0"
    
    return headers


# ─── API Calls ──────────────────────────────────────────────────

async def validate_session(cookies: dict) -> dict:
    """Validate cookies by loading flow.google.com and checking for auth markers."""
    from curl_cffi.requests import AsyncSession

    cookie_header = build_cookie_header(cookies)
    headers = {
        "Cookie": cookie_header,
        "User-Agent": BROWSER_FINGERPRINT_HEADERS["user-agent"],
    }

    async with AsyncSession() as session:
        try:
            resp = await session.get(
                "https://flow.google.com/",
                headers=headers,
                timeout=15,
                impersonate="chrome131",
                allow_redirects=True,
            )
            
            page_text = resp.text
            sapisid = cookies.get("SAPISID", {}).get("value", "")
            has_sapisid_in_page = sapisid and sapisid in page_text
            has_app_root = "<aisandbox-root>" in page_text
            
            email = None
            email_match = re.search(r'"email"\s*:\s*"([^"]+@[^"]+)"', page_text)
            if email_match:
                email = email_match.group(1)
            
            return {
                "valid": has_sapisid_in_page and has_app_root,
                "has_app_root": has_app_root,
                "has_sapisid": has_sapisid_in_page,
                "email": email,
                "status_code": resp.status_code,
            }
        except Exception as e:
            return {"valid": False, "error": str(e)}


async def api_call(
    cookies: dict,
    method: str,
    path: str,
    body: dict = None,
    api_key: str = None,
    timeout: int = 120,
) -> dict:
    """Make an authenticated API call to aisandbox-pa.googleapis.com using SAPISIDHASH.
    
    Args:
        cookies: Parsed cookies dict
        method: HTTP method (GET, POST)
        path: API path (e.g., "/v1/credits")
        body: Request body for POST
        api_key: API key (uses default if not provided)
        timeout: Request timeout in seconds
    
    Returns:
        {"success": bool, "status_code": int, "data": dict|None, "error": str|None}
    """
    import httpx
    
    if api_key is None:
        api_key = API_KEY
    
    headers = build_sapisidhash_headers(cookies)
    
    url = f"{API_BASE}{path}"
    if api_key:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}key={api_key}"
    
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            if method.upper() == "GET":
                resp = await client.get(url, headers=headers)
            else:
                content = json.dumps(body).encode("utf-8") if body else b""
                resp = await client.post(url, headers=headers, content=content)
            
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception:
                    data = {"raw": resp.text[:1000]}
                return {"success": True, "status_code": 200, "data": data}
            else:
                return {
                    "success": False,
                    "status_code": resp.status_code,
                    "error": resp.text[:500],
                }
        except Exception as e:
            return {"success": False, "status_code": 0, "error": str(e)}


async def get_credits(cookies: dict, api_key: str = None) -> dict:
    """Get credits for an account."""
    return await api_call(cookies, "GET", "/v1/credits", api_key=api_key)


async def generate_images(
    cookies: dict,
    project_id: str,
    prompt: str,
    model_name: str = "IMAGEN_3_5",
    aspect_ratio: str = "IMAGE_ASPECT_RATIO_LANDSCAPE",
    seed: int = None,
    image_inputs: list = None,
    api_key: str = None,
) -> dict:
    """Generate images via the Flow API using SAPISIDHASH auth."""
    import random
    import uuid
    
    if seed is None:
        seed = random.randint(1, 999999)
    
    session_id = f";{int(time.time() * 1000)}"
    batch_id = str(uuid.uuid4())
    
    client_context = {
        "sessionId": session_id,
        "projectId": project_id,
        "tool": "PINHOLE",
    }
    
    request_data = {
        "clientContext": client_context,
        "seed": seed,
        "imageModelName": model_name,
        "imageAspectRatio": aspect_ratio,
        "structuredPrompt": {"parts": [{"text": prompt}]},
        "imageInputs": image_inputs or [],
    }
    
    body = {
        "clientContext": client_context,
        "mediaGenerationContext": {"batchId": batch_id},
        "useNewMedia": True,
        "requests": [request_data],
    }
    
    return await api_call(
        cookies, "POST",
        f"/v1/projects/{project_id}/flowMedia:batchGenerateImages",
        body=body,
        api_key=api_key,
        timeout=120,
    )


async def generate_video(
    cookies: dict,
    project_id: str,
    prompt: str,
    model_name: str = "VEO_2",
    aspect_ratio: str = "VIDEO_ASPECT_RATIO_LANDSCAPE",
    duration: str = "VIDEO_DURATION_FIVE_SECONDS",
    seed: int = None,
    start_image_id: str = None,
    end_image_id: str = None,
    api_key: str = None,
) -> dict:
    """Generate video via the Flow API using SAPISIDHASH auth."""
    import random
    import uuid
    
    if seed is None:
        seed = random.randint(1, 999999)
    
    session_id = f";{int(time.time() * 1000)}"
    batch_id = str(uuid.uuid4())
    
    client_context = {
        "sessionId": session_id,
        "projectId": project_id,
        "tool": "AETHER",
    }
    
    request_data = {
        "clientContext": client_context,
        "seed": seed,
        "videoModelName": model_name,
        "videoAspectRatio": aspect_ratio,
        "duration": duration,
        "structuredPrompt": {"parts": [{"text": prompt}]},
    }
    
    if start_image_id:
        request_data["imageInputs"] = [{"name": start_image_id, "imageInputType": "IMAGE_INPUT_TYPE_REFERENCE"}]
    
    body = {
        "clientContext": client_context,
        "mediaGenerationContext": {"batchId": batch_id},
        "useNewMedia": True,
        "requests": [request_data],
    }
    
    return await api_call(
        cookies, "POST",
        f"/v1/projects/{project_id}/flowMedia:batchGenerateVideos",
        body=body,
        api_key=api_key,
        timeout=300,
    )


async def upload_image(
    cookies: dict,
    image_bytes: bytes,
    project_id: str = None,
    mime_type: str = "image/png",
    api_key: str = None,
) -> dict:
    """Upload an image asset via the Flow API."""
    import base64
    
    image_base64 = base64.b64encode(image_bytes).decode("utf-8")
    upload_file_name = f"upload_{int(time.time() * 1000)}.png"
    
    client_context = {"tool": "PINHOLE"}
    if project_id:
        client_context["projectId"] = project_id
    
    body = {
        "clientContext": client_context,
        "fileName": upload_file_name,
        "imageBytes": image_base64,
        "isHidden": False,
        "isUserUploaded": True,
        "mimeType": mime_type,
    }
    
    return await api_call(
        cookies, "POST",
        "/v1/flow/uploadImage",
        body=body,
        api_key=api_key,
    )


async def create_project(cookies: dict, title: str = "API Project", api_key: str = None) -> dict:
    """Create a new Flow project."""
    body = {
        "title": title,
        "tool": "PINHOLE",
    }
    return await api_call(
        cookies, "POST",
        "/v1/flow/createProject",
        body=body,
        api_key=api_key,
    )


# ─── CLI Entry Point ────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio

    if len(sys.argv) < 2:
        print("Usage: python sapisidhash_resolver.py <cookies_file> [test_credits|test_generate]")
        sys.exit(1)

    arg = sys.argv[1]
    action = sys.argv[2] if len(sys.argv) > 2 else "validate"
    
    if os.path.isfile(arg):
        with open(arg) as f:
            raw_cookies = f.read()
    else:
        raw_cookies = arg

    cookies = parse_cookies_auto(raw_cookies)
    if not cookies:
        print("ERROR: No cookies parsed from input")
        sys.exit(1)

    validation = validate_cookies(cookies)
    print(f"\n=== Cookie Validation ===")
    print(f"Total cookies: {validation['total_cookies']}")
    print(f"Essential present: {', '.join(validation['essential_present'])}")
    if validation['essential_missing']:
        print(f"Essential missing: {', '.join(validation['essential_missing'])}")
    print(f"Valid: {validation['valid']}")

    if action == "validate":
        print(f"\n=== Online Validation ===")
        online = asyncio.run(validate_session(cookies))
        print(f"Valid session: {online.get('valid')}")
        print(f"App root: {online.get('has_app_root')}")
        print(f"SAPISID in page: {online.get('has_sapisid')}")
        if online.get('email'):
            print(f"Email: {online['email']}")
        if online.get('error'):
            print(f"Error: {online['error']}")
    
    elif action == "test_credits":
        print(f"\n=== Testing Credits API ===")
        result = asyncio.run(get_credits(cookies))
        print(f"Success: {result['success']}")
        print(f"Status: {result.get('status_code')}")
        if result['success']:
            print(f"Data: {json.dumps(result['data'], indent=2)}")
        else:
            print(f"Error: {result.get('error', 'unknown')[:300]}")
    
    elif action == "test_generate":
        print(f"\n=== Testing Image Generation ===")
        # First need a project ID
        print("Creating project...")
        proj_result = asyncio.run(create_project(cookies))
        print(f"Project: {json.dumps(proj_result, indent=2)[:500]}")
