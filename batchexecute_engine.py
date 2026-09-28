import asyncio
import json
import logging
import re
import uuid
from typing import List

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger("engine")

class BatchExecuteEngine:
    def __init__(self):
        self.reqid = 0

    def get_page(self):
        import browser_manager
        if not browser_manager._browser_context or not browser_manager._browser_context.pages:
            raise Exception("Browser page not available. Is browser started?")
        return browser_manager._browser_context.pages[0]

    async def ensure_project_page(self) -> str:
        page = self.get_page()
        current_url = page.url
        
        # 1. Cek jika sudah berada di project URL
        if "flow.google.com/project/" in current_url:
            match = re.search(r'flow\.google\.com/project/([a-zA-Z0-9_-]+)', current_url)
            if match:
                pid = match.group(1)
                return pid

        # Pastikan navigasi ke project valid di Flow
        try:
            await page.goto("https://flow.google.com/project/0f21b6bf-24a7-4cfe-81ec-0e674b324f4a", wait_until="networkidle", timeout=15000)
        except Exception:
            pass
        return "0f21b6bf-24a7-4cfe-81ec-0e674b324f4a"

    async def ensure_token(self) -> str:
        page = self.get_page()
        
        # Check if ya29 token exists in cookie or WIZ data
        token = await page.evaluate('''() => {
            const match = document.cookie.match(/ya29\\.[\\.a-zA-Z0-9_-]+/);
            if (match) return match[0];
            return window.WIZ_global_data?.SNlM0e ? "valid_wiz_session" : null;
        }''')
        
        if token and token != "valid_wiz_session":
            logger.info(f"Token found: {token[:20]}...")
            return token
            
        # If no_flow_key or token missing, navigate to labs.google/fx/404 to refresh session and keys
        logger.warning("Token/Session key missing (no_flow_key). Refreshing session...")
        try:
            await page.goto("https://flow.google.com/project/0f21b6bf-24a7-4cfe-81ec-0e674b324f4a", wait_until="networkidle", timeout=15000)
            await asyncio.sleep(2)
        except Exception as e:
            logger.warning(f"Project refresh notice: {e}")
            
        return "refreshed"

    async def get_recaptcha_token(self, action='VIDEO_GENERATION') -> str:
        page = self.get_page()
        site_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
        try:
            token = await page.evaluate(f'''async () => {{
                if (typeof grecaptcha !== "undefined" && grecaptcha.enterprise && typeof grecaptcha.enterprise.execute === "function") {{
                    return await grecaptcha.enterprise.execute("{site_key}", {{action: "{action}"}});
                }}
                if (typeof grecaptcha !== "undefined" && typeof grecaptcha.execute === "function") {{
                    return await grecaptcha.execute("{site_key}", {{action: "{action}"}});
                }}
                return "dummy_recaptcha_token";
            }}''')
            if token and token != "dummy_recaptcha_token":
                logger.info(f"reCAPTCHA token acquired: {token[:30]}...")
                return token
        except Exception as e:
            logger.debug(f"reCAPTCHA evaluation notice: {e}")
        return "dummy_recaptcha_token"

    async def _send_batchexecute(self, rpcid: str, inner_data: list, project_id: str) -> str:
        page = self.get_page()
        xsrf = await page.evaluate('''() => window.WIZ_global_data?.SNlM0e || ""''')
        fsid = await page.evaluate('''() => window.WIZ_global_data?.FdrFJe || "-7000000000000000000"''')
        self.reqid = (self.reqid + 1) % 100000
        
        logger.info(f"XSRF: {xsrf[:30]}... | FSID: {fsid} | rpcid: {rpcid}")
        
        url = f"/_/AiSandboxAngularFrontend/data/batchexecute?rpcids={rpcid}&source-path=/project/{project_id}&bl=boq_labs-ai-sandbox-frontend_20260922.00_p0&f.sid={fsid}&hl=id&_reqid={self.reqid}&rt=c"
        
        inner_json_str = json.dumps(inner_data, separators=(',', ':'))
        freq_list = [[[rpcid, inner_json_str, None, "generic"]]]
        freq_json_str = json.dumps(freq_list, separators=(',', ':'))
        
        logger.debug(f"Sending {rpcid} with inner_data length: {len(inner_json_str)}")
        
        resp = await page.evaluate('''async ([freq_json_str, xsrf_token, url]) => {
            return new Promise((resolve, reject) => {
                const xhr = new XMLHttpRequest();
                xhr.open("POST", url, true);
                xhr.setRequestHeader("Content-Type", "application/x-www-form-urlencoded;charset=UTF-8");
                xhr.onload = () => resolve(xhr.responseText);
                xhr.onerror = () => reject(new Error("XHR failed: " + xhr.status));
                let body = "f.req=" + encodeURIComponent(freq_json_str);
                if (xsrf_token) body += "&at=" + encodeURIComponent(xsrf_token) + "&";
                xhr.send(body);
            });
        }''', [freq_json_str, xsrf, url])
        
        logger.info(f"{rpcid} RESPONSE (first 500 chars): {resp[:500]}")
        return resp

    def _parse_batchexecute_response(self, text: str) -> list:
        if text.startswith(")]}'"):
            text = text[4:]
        frames = []
        decoder = json.JSONDecoder()
        for raw in text.splitlines():
            line = raw.strip()
            if not line.startswith("["):
                continue
            try:
                decoded, _ = decoder.raw_decode(line)
                if isinstance(decoded, list):
                    for item in decoded:
                        if isinstance(item, list) and len(item) >= 3 and item[0] == "wrb.fr":
                            try:
                                frames.append((item[1], json.loads(item[2])))
                            except:
                                # item[2] might not be JSON string, check for error code
                                if len(item) >= 6 and isinstance(item[5], list):
                                    frames.append((item[1], {"error_code": item[5]}))
            except:
                pass
        logger.info(f"Parsed {len(frames)} frames")
        return frames

    async def upload_image_direct(self, image_path: str, project_id: str) -> str:
        """Upload image (from local path or remote URL) to Google Flow library directly via page.request (Zero UI). Returns mediaId."""
        import base64
        import requests as _requests
        from pathlib import Path
        
        img_bytes = b""
        if image_path.startswith("http://") or image_path.startswith("https://"):
            try:
                r = _requests.get(image_path, timeout=20)
                if r.status_code == 200:
                    img_bytes = r.content
            except Exception as e:
                logger.error(f"Failed to fetch remote reference image: {e}")
                
        if not img_bytes:
            p = Path(image_path)
            if p.exists():
                img_bytes = p.read_bytes()
                
        if not img_bytes:
            logger.warning(f"Reference image not accessible: {image_path}")
            return ""
            
        b64 = base64.b64encode(img_bytes).decode()
        page = self.get_page()
        
        url = "https://aisandbox-pa.googleapis.com/v1/flow/uploadImage"
        body = {
            "clientContext": {"projectId": project_id, "tool": "PINHOLE"},
            "imageBytes": b64
        }
        
        resp = await page.request.post(
            url,
            headers={"Content-Type": "application/json"},
            data=json.dumps(body)
        )
        if resp.status >= 400:
            logger.warning(f"Direct uploadImage returned HTTP {resp.status}")
            return ""
            
        res_json = await resp.json()
        media_id = res_json.get("media", {}).get("name") or res_json.get("mediaId") or ""
        logger.info(f"Direct uploaded image asset mediaId: {media_id}")
        return media_id

    async def generate_r2v_direct(
        self,
        prompt: str,
        image_path: str,
        model_key: str,
        aspect_ratio: int,
        count: int,
        project_id: str
    ) -> list:
        """Submit R2V/I2V generation directly via BatchExecute/XHR (Zero UI)."""
        media_id = await self.upload_image_direct(image_path, project_id)
        recaptcha_token = await self.get_recaptcha_token('VIDEO_GENERATION')
        
        requests_arr = []
        gen_uuids_out = []
        for _ in range(count):
            g1, g2 = str(uuid.uuid4()).upper(), str(uuid.uuid4()).upper()
            gen_uuids_out.append(g1)
            
            # Format according to captured Angular Frontend schema
            if media_id:
                req_item = [[None, None, [[[prompt]]]], model_key, aspect_ratio, None, [None, None, None, None, g1, g2], [None, [media_id]]]
            else:
                req_item = [[None, None, [[[prompt]]]], model_key, aspect_ratio, None, [None, None, None, None, g1, g2]]
            requests_arr.append(req_item)
            
        batch_id = str(uuid.uuid4()).upper()
        inner_data = [requests_arr, [None, 22, None, None, None, project_id, None, None, None, None, [recaptcha_token, 1]], [batch_id, count]]
        
        logger.info(f"Submitting Zero-UI R2V: model={model_key}, media_id={media_id}, aspect={aspect_ratio}")
        resp_text = await self._send_batchexecute("YhhmEf", inner_data, project_id)
        
        return gen_uuids_out

    async def generate_t2v(self, prompt: str, model_key: str, aspect_ratio: int, count: int, project_id: str) -> list:
        recaptcha_token = await self.get_recaptcha_token('VIDEO_GENERATION')
        requests_arr = []
        gen_uuids_out = []
        for _ in range(count):
            g1, g2 = str(uuid.uuid4()).upper(), str(uuid.uuid4()).upper()
            gen_uuids_out.append(g1)
            requests_arr.append([[None, None, [[[prompt]]]], model_key, aspect_ratio, None, [None, None, None, None, g1, g2]])
            
        batch_id = str(uuid.uuid4()).upper()
        inner_data = [requests_arr, [None, 22, None, None, None, project_id, None, None, None, None, [recaptcha_token, 1]], [batch_id, count]]
        
        logger.info(f"Submitting YhhmEf: model={model_key}, aspect={aspect_ratio}, count={count}, project={project_id}")
        resp_text = await self._send_batchexecute("YhhmEf", inner_data, project_id)
        
        frames = self._parse_batchexecute_response(resp_text)
        logger.info(f"YhhmEf frames: {frames}")
        
        # Check for error
        for rpcid, payload in frames:
            if rpcid == "YhhmEf":
                if isinstance(payload, dict) and "error_code" in payload:
                    raise Exception(f"BatchExecute returned error code: {payload['error_code']}. Full response: {resp_text[:500]}")
                if isinstance(payload, list) and len(payload) > 0 and str(payload[0]) == "er":
                    raise Exception(f"BatchExecute 400 Error. Response: {resp_text[:500]}")
        
        logger.info(f"Generated UUIDs: {gen_uuids_out}")
        return gen_uuids_out

    async def poll_generation(self, gen_uuids: list, project_id: str) -> dict:
        uuid_arr = [[u] for u in gen_uuids]
        inner_data = [None, None, uuid_arr]
        resp_text = await self._send_batchexecute("jwpduf", inner_data, project_id)
        
        frames = self._parse_batchexecute_response(resp_text)
        results = {}
        
        def as_list(node):
            return node if isinstance(node, list) else None
            
        def at(node, *path):
            curr = node
            for p in path:
                items = as_list(curr)
                if items is None or p >= len(items):
                    return None
                curr = items[p]
            return curr

        def is_record(node):
            if not isinstance(node, list) or len(node) < 6 or node[3] != "CAE":
                return False
            return isinstance(node[0], str) and isinstance(node[1], str) and isinstance(node[2], str)

        def find_records(node):
            if isinstance(node, list):
                if is_record(node):
                    media_id = node[2]
                    status_cell = as_list(at(node, 5, 8))
                    status = status_cell[0] if status_cell else None
                    video_url = at(node, 7, 0, 8)
                    poster_url = at(node, 5, 10)
                    size_bytes = at(node, 5, 13)
                    
                    # Direct check if video_url is valid http
                    valid_video = video_url if isinstance(video_url, str) and video_url.startswith("http") else None
                    valid_poster = poster_url if isinstance(poster_url, str) and poster_url.startswith("http") else None
                    
                    results[media_id] = {
                        "media_id": media_id,
                        "status": status,
                        "is_done": status == 3,
                        "is_running": status in (2, 6),
                        "video_url": valid_video,
                        "poster_url": valid_poster,
                        "size_bytes": size_bytes
                    }
                else:
                    for child in node:
                        find_records(child)

        for rpcid, payload in frames:
            if rpcid in ("jwpduf", "as29s", "YhhmEf"):
                find_records(payload)
                
                # Extract any signed video URLs from raw payload strings
                def extract_signed_urls(node):
                    if isinstance(node, str) and "https://" in node and ("Signature=" in node or "Expires=" in node):
                        for gid in gen_uuids:
                            if gid in str(payload):
                                if "video" in node or node.endswith(".mp4") or "googlevideo" in node:
                                    if gid in results:
                                        results[gid]["video_url"] = node
                                elif "image" in node or node.endswith(".jpg"):
                                    if gid in results:
                                        results[gid]["poster_url"] = node
                    elif isinstance(node, list):
                        for item in node:
                            extract_signed_urls(item)
                extract_signed_urls(payload)
                
        return results

    async def download_video_mp4(self, media_id: str) -> tuple[str, str]:
        """
        Fetch direct signed GCS MP4 URL via page.request.get(media.getMediaUrlRedirect).
        Saves MP4 file locally and returns (local_file_path, signed_cdn_mp4_url).
        """
        page = self.get_page()
        url = f"https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name={media_id}"
        
        try:
            # page.request carries all session cookies and handles 302 redirect to signed GCS .mp4
            resp = await page.request.get(url, max_redirects=5, timeout=120000)
            final_signed_url = resp.url
            
            import os
            from pathlib import Path
            out_dir = Path("/root/api-google-flow/output")
            out_dir.mkdir(parents=True, exist_ok=True)
            
            target_path = str(out_dir / f"{media_id}.mp4")
            body = await resp.body()
            
            with open(target_path, "wb") as f:
                f.write(body)
                
            logger.info(f"Downloaded MP4 video ({len(body)} bytes) to {target_path}")
            return target_path, final_signed_url
        except Exception as e:
            logger.error(f"Failed to download MP4 video for {media_id}: {e}")
            return "", f"https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name={media_id}"
