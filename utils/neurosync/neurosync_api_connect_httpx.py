import logging
import httpx
import json

API_KEY = "YOUR-NEUROSYNC-API-KEY"  # Your API key
REMOTE_URL = "https://yp5asfk17r1rcy-5000.proxy.runpod.net/neurosync/audio_to_blendshapes"  # External API URL
LOCAL_URL = "http://127.0.0.1:5000/neurosync/audio_to_blendshapes"  # Local URL

# 创建全局 Client 实例，指定连接池参数
limits = httpx.Limits(max_connections=15, max_keepalive_connections=5)
global_client = httpx.Client(limits=limits)


def send_audio_to_neurosync(audio_bytes, use_local=False):
    try:
        # 根据标志选择本地或远程 URL
        url = LOCAL_URL if use_local else REMOTE_URL
        headers = {}
        if not use_local:
            headers["API-Key"] = API_KEY

        response = post_audio_bytes(audio_bytes, url, headers)
        response.raise_for_status()  # 若状态码非 2xx，将抛出异常
        json_response = response.json()
        return parse_blendshapes_from_json(json_response)

    except httpx.RequestError as e:
        logging.error(f"Request error: {e}")
        return None
    except json.JSONDecodeError as e:
        logging.error(f"JSON parsing error: {e}")
        return None

def validate_audio_bytes(audio_bytes):
    return audio_bytes is not None and len(audio_bytes) > 0

def post_audio_bytes(audio_bytes, url, headers):
    headers["Content-Type"] = "application/octet-stream"
    response = global_client.post(url, headers=headers, content=audio_bytes, timeout=60.0)
    return response

def parse_blendshapes_from_json(json_response):
    blendshapes = json_response.get("blendshapes", [])
    facial_data = []
    for frame in blendshapes:
        frame_data = [float(value) for value in frame]
        facial_data.append(frame_data)
    return facial_data
