import json
import struct
import uuid
import gzip
import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger("webchat-api")

TTS_V1_URL = "wss://openspeech.bytedance.com/api/v1/tts/ws_binary"

MSG_FULL_CLIENT = 0b0001
MSG_FULL_SERVER = 0b1001
MSG_AUDIO_ONLY = 0b1011
MSG_ERROR = 0b1111

FLAG_NO_SEQ = 0b0000
FLAG_SEQ_POS = 0b0001
FLAG_SEQ_NEG = 0b0010

SERIAL_JSON = 0b0001
COMPRESS_NONE = 0b0000
COMPRESS_GZIP = 0b0001


def _build_v1_request(appid: str, token: str, text: str, speaker: str,
                      cluster: str = "volcano_tts", encoding: str = "mp3",
                      speed: float = 1.0, operation: str = "submit") -> bytes:
    reqid = str(uuid.uuid4())
    request_json = {
        "app": {
            "appid": appid,
            "token": token,
            "cluster": cluster,
        },
        "user": {
            "uid": "doubao_podcast"
        },
        "audio": {
            "voice_type": speaker,
            "encoding": encoding,
            "speed_ratio": speed,
            "volume_ratio": 1.0,
            "pitch_ratio": 1.0,
        },
        "request": {
            "reqid": reqid,
            "text": text,
            "text_type": "plain",
            "operation": operation,
        }
    }
    payload = json.dumps(request_json, ensure_ascii=False).encode("utf-8")
    b0 = (0b0001 << 4) | 0b0001
    b1 = (MSG_FULL_CLIENT << 4) | FLAG_NO_SEQ
    b2 = (SERIAL_JSON << 4) | COMPRESS_NONE
    b3 = 0b00000000
    header = bytes([b0, b1, b2, b3])
    frame = bytearray(header)
    frame.extend(struct.pack(">I", len(payload)))
    frame.extend(payload)
    return bytes(frame)


def _parse_v1_frame(data: bytes) -> dict:
    if len(data) < 4:
        return {"error": "too short"}
    b0, b1, b2, b3 = data[0], data[1], data[2], data[3]
    header_size = (b0 & 0x0F) * 4
    msg_type = b1 >> 4
    flags = b1 & 0x0F
    serial = b2 >> 4
    compress = b2 & 0x0F

    pos = header_size

    seq_num = None
    if msg_type == MSG_AUDIO_ONLY and (flags & 0x01 or flags & 0x02):
        if pos + 4 <= len(data):
            seq_num = struct.unpack(">i", data[pos:pos + 4])[0]
            pos += 4

    payload = b""
    if pos + 4 <= len(data):
        payload_len = struct.unpack(">I", data[pos:pos + 4])[0]
        pos += 4
        if pos + payload_len <= len(data):
            payload = data[pos:pos + payload_len]

    if compress == COMPRESS_GZIP and payload:
        try:
            payload = gzip.decompress(payload)
        except Exception:
            pass

    if msg_type == MSG_ERROR and not payload and len(data) > header_size + 4:
        try:
            skip = header_size + 4
            inner_len = struct.unpack(">I", data[header_size:header_size + 4])[0]
            if header_size + 4 + inner_len <= len(data):
                payload = data[header_size + 4:header_size + 4 + inner_len]
        except Exception:
            pass
        if not payload:
            for offset in range(header_size, min(header_size + 16, len(data))):
                try:
                    payload = data[offset:]
                    json.loads(payload)
                    break
                except Exception:
                    payload = b""
                    continue

    return {
        "msg_type": msg_type,
        "flags": flags,
        "serial": serial,
        "compress": compress,
        "seq_num": seq_num,
        "payload": payload,
    }


async def volcengine_tts(
    text: str,
    output_path: str,
    app_key: str,
    access_key: str,
    speaker: str = "zh_female_wenroutaozi_uranus_bigtts",
    audio_format: str = "mp3",
    speed: float = 1.0,
    cluster: str = "volcano_tts",
) -> dict:
    import websockets

    audio_chunks = bytearray()
    result_info = {"success": False, "bytes": 0, "error": None}

    try:
        frame = _build_v1_request(app_key, access_key, text, speaker,
                                  cluster, audio_format, speed)

        async with websockets.connect(TTS_V1_URL, ping_interval=20,
                                      close_timeout=10) as ws:
            await ws.send(frame)

            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                except asyncio.TimeoutError:
                    result_info["error"] = "WebSocket timeout"
                    break

                if not isinstance(raw, bytes):
                    continue

                parsed = _parse_v1_frame(raw)
                msg_type = parsed.get("msg_type")
                flags = parsed.get("flags")
                payload = parsed.get("payload", b"")

                if msg_type == MSG_ERROR:
                    try:
                        err = json.loads(payload)
                        result_info["error"] = f"TTS error: {json.dumps(err, ensure_ascii=False)[:300]}"
                    except Exception:
                        result_info["error"] = f"TTS error: {payload[:200].decode('utf-8', errors='replace')}"
                    logger.error(f"[VolcTTS] {result_info['error']}")
                    break

                if msg_type == MSG_FULL_SERVER:
                    if payload:
                        try:
                            resp = json.loads(payload)
                            logger.debug(f"[VolcTTS] Server response: {json.dumps(resp, ensure_ascii=False)[:200]}")
                        except Exception:
                            pass
                    continue

                if msg_type == MSG_AUDIO_ONLY:
                    if payload:
                        audio_chunks.extend(payload)
                    if flags in (FLAG_SEQ_NEG, 0b0011):
                        break
                    continue

        if audio_chunks:
            with open(output_path, "wb") as f:
                f.write(audio_chunks)
            result_info["success"] = True
            result_info["bytes"] = len(audio_chunks)
            logger.info(f"[VolcTTS] Audio saved: {len(audio_chunks)} bytes -> {output_path}")
        else:
            if not result_info["error"]:
                result_info["error"] = "No audio data received"
            logger.warning(f"[VolcTTS] No audio: {result_info['error']}")

    except Exception as e:
        result_info["error"] = str(e)
        logger.error(f"[VolcTTS] Error: {e}")

    return result_info


async def volcengine_tts_segmented(
    segments: list,
    output_path: str,
    app_key: str,
    access_key: str,
    speaker_map: Optional[dict] = None,
    audio_format: str = "mp3",
    cluster: str = "volcano_tts",
) -> dict:
    if not speaker_map:
        speaker_map = {
            "host1": "zh_female_wenroutaozi_uranus_bigtts",
            "host2": "zh_male_chunhou_uranus_bigtts",
            "both": "zh_female_wenroutaozi_uranus_bigtts",
            "default": "zh_female_wenroutaozi_uranus_bigtts",
        }

    all_audio = bytearray()
    result_info = {"success": False, "bytes": 0, "error": None, "segments": 0}

    for i, seg in enumerate(segments):
        speaker_key = seg.get("speaker", "default")
        text = seg.get("text", "").strip()
        if not text:
            continue

        speaker = speaker_map.get(speaker_key,
                                 speaker_map.get("default", "zh_female_wenroutaozi_uranus_bigtts"))

        logger.info(f"[VolcTTS] Segment {i+1}/{len(segments)}: "
                     f"speaker={speaker_key}({speaker}), text={text[:50]}...")

        seg_result = await volcengine_tts(
            text=text,
            output_path=output_path + f".seg{i}",
            app_key=app_key,
            access_key=access_key,
            speaker=speaker,
            audio_format=audio_format,
            cluster=cluster,
        )

        if seg_result["success"]:
            with open(output_path + f".seg{i}", "rb") as f:
                all_audio.extend(f.read())
            try:
                os.remove(output_path + f".seg{i}")
            except Exception:
                pass
            result_info["segments"] += 1
        else:
            logger.warning(f"[VolcTTS] Segment {i+1} failed: {seg_result.get('error', '')}")

    if all_audio:
        with open(output_path, "wb") as f:
            f.write(all_audio)
        result_info["success"] = True
        result_info["bytes"] = len(all_audio)
        logger.info(f"[VolcTTS] Segmented audio saved: {len(all_audio)} bytes -> {output_path}")
    else:
        result_info["error"] = "No audio data received from any segment"

    return result_info


async def get_tts_credentials(account: dict) -> dict:
    from config import CONFIG, USER_AGENT, SIGN_METHOD
    from sse import build_url_params, generate_x_flow_trace
    import aiohttp

    cookie_str = account.get('cookie', CONFIG.get('cookie', ''))
    base_url = CONFIG.get('api_base', 'https://www.doubao.com')

    url = f"{base_url}/alice/user/launch"

    full_url = None
    if SIGN_METHOD == 'b2' and CONFIG.get('_signer') and CONFIG['_signer']._initialized:
        signer_obj = CONFIG['_signer']
        base_params = {
            "aid": "497858",
            "device_platform": "web",
            "language": "zh",
            "pc_version": "3.17.3",
            "pkg_type": "release_version",
            "real_aid": "497858",
            "region": "CN",
            "samantha_web": "1",
            "sys_region": "CN",
            "use-olympus-account": "1",
            "version_code": "20800",
        }
        try:
            signed_url = await signer_obj.get_signed_url(url, base_params)
            if signed_url:
                full_url = signed_url
                logger.info("[VolcTTS] Using B2 signed URL for launch API")
        except Exception:
            pass

    if not full_url:
        params = build_url_params(account)
        full_url = f"{url}?{params}"

    headers = {
        'content-type': 'application/json',
        'cookie': cookie_str,
        'origin': 'https://www.doubao.com',
        'referer': 'https://www.doubao.com/',
        'user-agent': USER_AGENT,
        'x-flow-trace': generate_x_flow_trace(),
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(full_url, json={}, headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.error(f"[VolcTTS] Launch API returned {resp.status}")
                    return {}
                data = await resp.json()
                if data.get("code") != 0:
                    logger.error(f"[VolcTTS] Launch API error: {data.get('msg', '')}")
                    return {}
                config = data.get("data", {}).get("config", {})
                return {
                    "app_key": config.get("audio_app_key", ""),
                    "access_key": config.get("audio_token", ""),
                    "enterprise_app_key": config.get("enterprise_audio_app_key", ""),
                }
    except Exception as e:
        logger.error(f"[VolcTTS] Failed to get TTS credentials: {e}")
        return {}
