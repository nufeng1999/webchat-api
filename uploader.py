import json
import os
import sys
import uuid
import hashlib
import binascii
import logging
import base64
import aiohttp
import httpx
from requests_aws4auth import AWS4Auth
from fastapi import HTTPException
from typing import Optional

logger = logging.getLogger("webchat-api.uploader")

if sys.platform.startswith("win"):
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
elif sys.platform == "darwin":
    USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
else:
    USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"


def _load_config():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


async def upload_image(file_data: bytes, file_name: str, cookie: str, device_id: str = "",
                       tea_uuid: str = "", web_id: str = "") -> dict:
    """
    Upload image to Doubao's ImageX service.
    Returns attachment dict for use in chat messages.

    Flow:
    1. prepare_upload → Get AWS credentials
    2. ApplyImageUpload → Get store URL
    3. Upload binary to TOS
    4. CommitImageUpload → Confirm, get ImageUri
    """
    config = _load_config()

    params = "&".join([
        "aid=497858",
        f"device_id={device_id or config.get('device_id', '')}",
        "device_platform=web",
        "language=zh",
        "pc_version=2.20.0",
        "pkg_type=release_version",
        "real_aid=497858",
        "region=CN",
        "samantha_web=1",
        "sys_region=CN",
        f"tea_uuid={tea_uuid or config.get('tea_uuid', '')}",
        "use-olympus-account=1",
        "version_code=20800",
    ])

    default_headers = {
        'content-type': 'application/json',
        'cookie': cookie or config.get('cookie', ''),
        'origin': 'https://www.doubao.com',
        'referer': 'https://www.doubao.com/chat/',
        'user-agent': USER_AGENT
    }

    async with httpx.AsyncClient(timeout=60) as client:
        # Step 1: Prepare Upload
        prepare_url = f"https://www.doubao.com/alice/resource/prepare_upload?{params}"
        prepare_payload = {
            "resource_type": 2,
            "scene_id": "5",
            "tenant_id": "5"
        }
        resp = await client.post(url=prepare_url, headers=default_headers, json=prepare_payload)
        prepare_data = resp.json()

        if prepare_data.get("code") != 0:
            raise HTTPException(status_code=500, detail=f"Prepare upload failed: {prepare_data}")

        upload_info = prepare_data.get("data", {})
        service_id = upload_info.get("service_id")
        upload_auth = upload_info.get("upload_auth_token", {})
        session_token = upload_auth.get("session_token")
        access_key = upload_auth.get("access_key")
        secret_key = upload_auth.get("secret_key")

        if not all([service_id, access_key, secret_key]):
            raise HTTPException(status_code=500, detail=f"Missing upload credentials: {upload_info}")

        # Step 2: Apply Image Upload
        if '.' not in file_name:
            file_name += '.png'
        file_ext = os.path.splitext(file_name)[1]
        file_size = len(file_data)

        apply_url = (
            f"https://imagex.bytedanceapi.com/?Action=ApplyImageUpload"
            f"&Version=2018-08-01&ServiceId={service_id}"
            f"&NeedFallback=true&FileSize={file_size}&FileExtension={file_ext}"
        )

        auth = AWS4Auth(access_key, secret_key, 'cn-north-1', "imagex", session_token=session_token)
        apply_request = client.build_request(
            method="GET",
            url=apply_url,
            headers={
                "origin": "https://www.doubao.com",
                "referer": "https://www.doubao.com",
                "user-agent": USER_AGENT,
            }
        )
        auth.__call__(apply_request)
        resp = await client.send(apply_request)
        data = resp.json()

        upload_address = data.get("Result", {}).get("UploadAddress", {})
        store_infos = upload_address.get("StoreInfos", [])
        if not store_infos:
            raise HTTPException(status_code=500, detail=f"Apply upload returned empty StoreInfos: {data}")

        store_info = store_infos[0]
        store_url = store_info.get("StoreUri")
        store_auth = store_info.get("Auth")
        session_key = upload_address.get("SessionKey")

        # Step 3: Upload Binary
        upload_url = f"https://tos-d-x-hl.snssdk.com/upload/v1/{store_url}"
        crc32 = format(binascii.crc32(file_data) & 0xFFFFFFFF, '08x')
        upload_headers = {
            "authorization": store_auth,
            "origin": "https://www.doubao.com",
            "referer": "https://www.doubao.com",
            "host": "tos-d-x-hl.snssdk.com",
            "content-type": "application/octet-stream",
            "content-disposition": 'attachment; filename="undefined"',
            "content-crc32": crc32
        }
        resp = await client.post(upload_url, content=file_data, headers=upload_headers)
        data = resp.json()
        if data.get("message") != "Success":
            raise HTTPException(status_code=500, detail=f"Upload failed: {data}")

        # Step 4: Commit Upload
        commit_url = (
            f"https://imagex.bytedanceapi.com/?Action=CommitImageUpload"
            f"&Version=2018-08-01&ServiceId={service_id}"
        )
        commit_payload = {"SessionKey": session_key}
        commit_headers = {
            "origin": "https://www.doubao.com",
            "referer": "https://www.doubao.com/",
            "user-agent": USER_AGENT,
        }

        commit_request = client.build_request(
            method="POST",
            url=commit_url,
            headers=commit_headers,
            json=commit_payload
        )
        auth.__call__(commit_request)
        resp = await client.send(commit_request)
        data = resp.json()

        results = data.get("Result", {}).get("PluginResult", [])
        if not results:
            raise HTTPException(status_code=500, detail=f"Commit upload returned empty PluginResult: {data}")

        result = results[0]
        image_uri = result.get("ImageUri")
        image_width = result.get("ImageWidth", 0)
        image_height = result.get("ImageHeight", 0)
        image_md5 = result.get("ImageMd5") or hashlib.md5(file_data).hexdigest()
        image_size = result.get("ImageSize", file_size)

        attachment = {
            "key": image_uri,
            "name": file_name,
            "type": "image",
            "file_review_state": 3,
            "file_parse_state": 3,
            "identifier": str(uuid.uuid4()),
            "option": {
                "height": image_height,
                "width": image_width
            },
            "md5": image_md5,
            "size": image_size
        }

        logger.info(f"Image uploaded: {file_name} ({image_width}x{image_height}, {image_size} bytes)")
        return attachment


async def upload_document(file_data: bytes, file_name: str, cookie: str, device_id: str = "",
                          tea_uuid: str = "", web_id: str = "") -> dict:
    """
    Upload a document (e.g. .json/.txt) to Doubao's ImageX service.
    Returns a new-protocol attachment dict (type=3) for content_block (block_type 10052).

    Flow mirrors upload_image but uses resource_type=1 (document) and reads the
    upload host from the ApplyImageUpload response (UploadHosts[0]).
    """
    config = _load_config()

    params = "&".join([
        "aid=497858",
        f"device_id={device_id or config.get('device_id', '')}",
        "device_platform=web",
        "language=zh",
        "pc_version=2.20.0",
        "pkg_type=release_version",
        "real_aid=497858",
        "region=CN",
        "samantha_web=1",
        "sys_region=CN",
        f"tea_uuid={tea_uuid or config.get('tea_uuid', '')}",
        "use-olympus-account=1",
        "version_code=20800",
    ])

    default_headers = {
        'content-type': 'application/json',
        'cookie': cookie or config.get('cookie', ''),
        'origin': 'https://www.doubao.com',
        'referer': 'https://www.doubao.com/chat/',
        'user-agent': USER_AGENT
    }

    async with httpx.AsyncClient(timeout=60) as client:
        # Step 1: Prepare Upload (resource_type=1 for documents)
        prepare_url = f"https://www.doubao.com/alice/resource/prepare_upload?{params}"
        prepare_payload = {
            "resource_type": 1,
            "scene_id": "5",
            "tenant_id": "5"
        }
        resp = await client.post(url=prepare_url, headers=default_headers, json=prepare_payload)
        prepare_data = resp.json()

        if prepare_data.get("code") != 0:
            raise HTTPException(status_code=500, detail=f"Prepare upload failed: {prepare_data}")

        upload_info = prepare_data.get("data", {})
        service_id = upload_info.get("service_id")
        upload_auth = upload_info.get("upload_auth_token", {})
        session_token = upload_auth.get("session_token")
        access_key = upload_auth.get("access_key")
        secret_key = upload_auth.get("secret_key")

        if not all([service_id, access_key, secret_key]):
            raise HTTPException(status_code=500, detail=f"Missing upload credentials: {upload_info}")

        # Step 2: Apply Image Upload
        if '.' not in file_name:
            file_name += '.json'
        file_ext = os.path.splitext(file_name)[1]
        file_size = len(file_data)

        apply_url = (
            f"https://imagex.bytedanceapi.com/?Action=ApplyImageUpload"
            f"&Version=2018-08-01&ServiceId={service_id}"
            f"&NeedFallback=true&FileSize={file_size}&FileExtension={file_ext}"
        )

        auth = AWS4Auth(access_key, secret_key, 'cn-north-1', "imagex", session_token=session_token)
        apply_request = client.build_request(
            method="GET",
            url=apply_url,
            headers={
                "origin": "https://www.doubao.com",
                "referer": "https://www.doubao.com",
                "user-agent": USER_AGENT,
            }
        )
        auth.__call__(apply_request)
        resp = await client.send(apply_request)
        data = resp.json()

        upload_address = data.get("Result", {}).get("UploadAddress", {})
        store_infos = upload_address.get("StoreInfos", [])
        if not store_infos:
            raise HTTPException(status_code=500, detail=f"Apply upload returned empty StoreInfos: {data}")

        store_info = store_infos[0]
        store_url = store_info.get("StoreUri")
        store_auth = store_info.get("Auth")
        session_key = upload_address.get("SessionKey")

        upload_hosts = upload_address.get("UploadHosts", [])
        upload_host = upload_hosts[0] if upload_hosts else "tos-d-x-hl.snssdk.com"

        # Step 3: Upload Binary (host comes from ApplyImageUpload response)
        upload_url = f"https://{upload_host}/upload/v1/{store_url}"
        crc32 = format(binascii.crc32(file_data) & 0xFFFFFFFF, '08x')
        upload_headers = {
            "authorization": store_auth,
            "origin": "https://www.doubao.com",
            "referer": "https://www.doubao.com",
            "host": upload_host,
            "content-type": "application/octet-stream",
            "content-disposition": 'attachment; filename="undefined"',
            "content-crc32": crc32
        }
        resp = await client.post(upload_url, content=file_data, headers=upload_headers)
        data = resp.json()
        if data.get("message") != "Success":
            raise HTTPException(status_code=500, detail=f"Upload failed: {data}")

        # Step 4: Commit Upload
        commit_url = (
            f"https://imagex.bytedanceapi.com/?Action=CommitImageUpload"
            f"&Version=2018-08-01&ServiceId={service_id}"
        )
        commit_payload = {"SessionKey": session_key}
        commit_headers = {
            "origin": "https://www.doubao.com",
            "referer": "https://www.doubao.com/",
            "user-agent": USER_AGENT,
        }

        commit_request = client.build_request(
            method="POST",
            url=commit_url,
            headers=commit_headers,
            json=commit_payload
        )
        auth.__call__(commit_request)
        resp = await client.send(commit_request)
        data = resp.json()

        results = data.get("Result", {}).get("PluginResult", [])
        if not results:
            raise HTTPException(status_code=500, detail=f"Commit upload returned empty PluginResult: {data}")

        result = results[0]
        file_uri = result.get("ImageUri") or result.get("SourceUri")
        file_size_final = result.get("ImageSize", file_size)

        # 尝试从服务器获取 attachment identifier，否则使用随机 UUID
        identifier = (
            result.get("AttachmentId") or
            result.get("AttachmentID") or
            result.get("Identifier") or
            str(uuid.uuid4())
        )

        attachment = {
            "type": 3,
            "identifier": identifier,
            "file": {
                "uri": file_uri,
                "url": "",
                "file_type": 0,
                "name": file_name,
                "size": file_size_final
            },
            "parse_state": 1,
            "review_state": 1,
            "upload_status": 1,
            "progress": 100,
            "src": ""
        }

        logger.info(f"Document uploaded: {file_name} ({file_size_final} bytes, uri={file_uri})")
        
        # 保存到 logs 目录供调试（带时间戳）
        try:
            from datetime import datetime
            logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
            os.makedirs(logs_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            saved_path = os.path.join(logs_dir, f"messages_{ts}.json")
            with open(saved_path, "wb") as f:
                f.write(file_data)
            logger.info(f"Document saved to {saved_path}")
        except Exception as e:
            logger.warning(f"Failed to save document to logs: {e}")
        
        return attachment


async def download_image(url: str) -> tuple[bytes, str]:
    """Download image from URL, returns (file_data, file_name)"""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url, headers={"user-agent": USER_AGENT})
        if resp.status_code != 200:
            raise HTTPException(status_code=400, detail=f"Failed to download image: {resp.status_code}")

        content_type = resp.headers.get("content-type", "")
        ext_map = {
            "image/jpeg": ".jpg", "image/png": ".png",
            "image/gif": ".gif", "image/webp": ".webp",
            "image/bmp": ".bmp"
        }
        ext = ext_map.get(content_type.split(";")[0], ".jpg")
        file_name = f"upload_{uuid.uuid4().hex[:8]}{ext}"
        return resp.content, file_name


def decode_base64_image(data_url: str) -> tuple[bytes, str]:
    """Decode base64 data URL, returns (file_data, file_name)"""
    if not data_url.startswith("data:"):
        raise HTTPException(status_code=400, detail="Invalid data URL format")

    try:
        header, encoded = data_url.split(",", 1)
        mime_part = header.split(";")[0]  # data:image/png
        mime = mime_part.replace("data:", "")

        ext_map = {
            "image/jpeg": ".jpg", "image/png": ".png",
            "image/gif": ".gif", "image/webp": ".webp",
            "image/bmp": ".bmp"
        }
        ext = ext_map.get(mime, ".png")
        file_name = f"upload_{uuid.uuid4().hex[:8]}{ext}"
        file_data = base64.b64decode(encoded)
        return file_data, file_name
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to decode base64 image: {e}")


async def process_image_url(image_url: str, cookie: str, device_id: str = "",
                           tea_uuid: str = "", web_id: str = "") -> dict:
    """Process an image URL or base64 data URL and upload to Doubao."""
    if image_url.startswith("data:"):
        file_data, file_name = decode_base64_image(image_url)
    else:
        file_data, file_name = await download_image(image_url)

    return await upload_image(
        file_data=file_data,
        file_name=file_name,
        cookie=cookie,
        device_id=device_id,
        tea_uuid=tea_uuid,
        web_id=web_id
    )
