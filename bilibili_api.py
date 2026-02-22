import requests
import time
import hashlib
import uuid
import logging
from typing import Dict, Tuple, Optional

log = logging.getLogger(__name__)

def parse_cookies(cookie_str: str) -> Dict[str, str]:
    cookies = {}
    for item in cookie_str.split(';'):
        item = item.strip()
        if '=' in item:
            key, value = item.split('=', 1)
            cookies[key] = value
    return cookies

def get_csrf_from_cookies(cookies: Dict[str, str]) -> str:
    return cookies.get('bili_jct', '')

def generate_sign(params: dict, appkey: str) -> str:
    param_keys = sorted(params.keys())
    query = '&'.join([f"{k}={params[k]}" for k in param_keys])
    sign_str = query + appkey
    return hashlib.md5(sign_str.encode('utf-8')).hexdigest()

def generate_trace_id() -> str:
    return f"PC_LINK:{str(uuid.uuid4()).upper()}:{int(time.time() * 1000)}"

class BilibiliAPI:
    def __init__(self, room_id: str, cookie_str: str):
        self.room_id = str(room_id)
        self.cookie_str = cookie_str
        self.parsed_cookies = parse_cookies(cookie_str)
        self.csrf = get_csrf_from_cookies(self.parsed_cookies)
        
        self.appkey = 'aae92bc66f3edfab'
        self.common_headers = {
            'User-Agent': 'LiveHime/7.7.0.8681 os/Windows pc_app/livehime build/8681 osVer/10.0_x86_64',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Cookie': self.cookie_str,
            'Connection': 'keep-alive',
        }

    def _get_base_data(self) -> dict:
        return {
            'room_id': self.room_id,
            'platform': 'pc_link',
            'csrf_token': self.csrf,
            'csrf': self.csrf,
            'access_key': '',
            'appkey': self.appkey,
            'build': '8681',
            'version': '7.7.0.8681',
            'ts': int(time.time()),
        }

    def start_live(self) -> Tuple[bool, Optional[str], Optional[str], str]:
        """
        开启直播房
        :return: (is_success, rtmp_url, stream_key, message)
        """
        if not self.csrf:
            return False, None, None, "Cookie 中缺少 bili_jct (csrf)，无法鉴权"

        data = self._get_base_data()
        data['area_v2'] = '624'  # 默认单机游戏分类，可在此更改
        data['type'] = '2'
        data['backup_stream'] = '0'
        data['sign'] = generate_sign(data, self.appkey)

        headers = {**self.common_headers, 'X-Event-TraceID': generate_trace_id()}
        
        try:
            resp = requests.post(
                'https://api.live.bilibili.com/room/v1/Room/startLive',
                headers=headers,
                data=data,
                timeout=10
            ).json()
            
            if resp.get('code') == 0:
                rtmp_addr = resp['data']['rtmp']['addr']
                stream_key = resp['data']['rtmp']['code']
                log.info("B 站开播成功! RTMP: %s", rtmp_addr)
                return True, rtmp_addr, stream_key, "开播成功"
            else:
                msg = resp.get('message') or resp.get('msg') or str(resp)
                log.error("B 站开播失败: %s", msg)
                return False, None, None, f"接口返回错误: {msg}"
        except Exception as e:
            log.exception("请求 B 站开播接口异常")
            return False, None, None, f"请求异常: {str(e)}"

    def stop_live(self) -> Tuple[bool, str]:
        """
        关闭直播房
        :return: (is_success, message)
        """
        if not self.csrf:
            return False, "Cookie 中缺少 csrf"
            
        data = self._get_base_data()
        data['sign'] = generate_sign(data, self.appkey)
        
        headers = {**self.common_headers, 'X-Event-TraceID': generate_trace_id()}
        
        try:
            resp = requests.post(
                'https://api.live.bilibili.com/room/v1/Room/stopLive',
                headers=headers,
                data=data,
                timeout=10
            ).json()
            
            if resp.get('code') == 0:
                log.info("B 站关播成功!")
                return True, "关播成功"
            else:
                msg = resp.get('message') or resp.get('msg') or str(resp)
                log.error("B 站关播失败: %s", msg)
                return False, f"接口返回错误: {msg}"
        except Exception as e:
            log.exception("请求 B 站关播接口异常")
            return False, f"请求异常: {str(e)}"
