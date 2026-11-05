"""后台联网校时；聊天只读取单调时钟推进的 UTC 缓存。"""
from __future__ import annotations

import asyncio
import socket
import struct
from email.utils import parsedate_to_datetime
from time import monotonic, time
from typing import Any


_NTP_EPOCH = 2_208_988_800
_NTP_SERVERS = ("ntp.aliyun.com", "time1.cloud.tencent.com", "time.cloudflare.com")
_HTTPS_TIME_URL = "https://www.cloudflare.com/cdn-cgi/trace"


class NetworkClock:
    def __init__(self, logger: Any, http: Any) -> None:
        self.logger=logger; self.http=http
        self.source="system"
        self._anchor:tuple[float,float]|None=None
        self._next_refresh=0.0
        self._last_warning:float|None=None
        self._lock=asyncio.Lock()

    def timestamp(self) -> float:
        if self._anchor is None:return time()
        utc,measured_at=self._anchor
        return utc+monotonic()-measured_at

    @staticmethod
    async def _query_ntp(host: str) -> tuple[float,float]:
        loop=asyncio.get_running_loop()
        addresses=await loop.getaddrinfo(host,123,family=socket.AF_INET,type=socket.SOCK_DGRAM)
        if not addresses:raise OSError("NTP 域名没有可用地址")
        request=bytearray(48); request[0]=0x23  # NTP v4 client.
        sent=time()+_NTP_EPOCH
        struct.pack_into("!II",request,40,int(sent)%2**32,int((sent%1)*2**32))
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
            sock.setblocking(False)
            await loop.sock_connect(sock,addresses[0][4])
            started=monotonic()
            await loop.sock_sendall(sock,request)
            response=await loop.sock_recv(sock,512)
            received_at=monotonic()
        if (len(response)<48 or response[0]>>6==3 or (response[0]>>3)&7 not in (3,4)
                or response[0]&7!=4 or not 1<=response[1]<=15
                or response[24:32]!=request[40:48]):
            raise ValueError("NTP 响应无效或服务未同步")
        receive_seconds,receive_fraction,send_seconds,send_fraction=struct.unpack_from("!IIII",response,32)
        if not any(response[32:40]) or not any(response[40:48]):
            raise ValueError("NTP 响应缺少时间戳")
        # 展开 2036 年的 NTP 秒数回绕，不依赖可能错误的系统年份。
        received=receive_seconds+(2**32 if receive_seconds<_NTP_EPOCH else 0)-_NTP_EPOCH+receive_fraction/2**32
        transmitted=send_seconds+(2**32 if send_seconds<_NTP_EPOCH else 0)-_NTP_EPOCH+send_fraction/2**32
        processing=transmitted-received; elapsed=received_at-started
        if processing<0 or processing>elapsed+0.001:
            raise ValueError("NTP 响应时间顺序无效")
        return transmitted+max(0,elapsed-processing)/2,received_at

    async def _query_https(self) -> tuple[float,float]:
        # 部分服务器禁止 UDP；HTTPS Date 作为秒级精度的备用来源。
        response=await self.http.request("GET",_HTTPS_TIME_URL,timeout=3,max_bytes=8192,
                                         headers={"Cache-Control":"no-cache"})
        if response.status!=200 or float(response.headers.get("age","0"))!=0:
            raise ValueError("HTTPS 时间响应无效或已缓存")
        current=parsedate_to_datetime(response.headers.get("date",""))
        if current.tzinfo is None:raise ValueError("HTTPS 时间缺少时区")
        return current.timestamp(),monotonic()

    async def refresh(self, force: bool = False) -> bool:
        async with self._lock:
            if not force and monotonic()<self._next_refresh:return self._anchor is not None
            for source in (*_NTP_SERVERS,_HTTPS_TIME_URL):
                try:
                    query=self._query_https() if source==_HTTPS_TIME_URL else self._query_ntp(source)
                    anchor=await asyncio.wait_for(query,timeout=3)
                except (OSError,ValueError,TypeError,OverflowError,RuntimeError):
                    continue
                self._anchor=anchor; self.source=source
                self._next_refresh=monotonic()+3600; self._last_warning=None
                return True
            now=monotonic(); self._next_refresh=now+300
            if self._last_warning is None or now-self._last_warning>=1800:
                self._last_warning=now
                fallback="上次校准时间" if self._anchor is not None else "系统 UTC 时间（尚未校准）"
                self.logger.warning(f"[MaiLife] 联网校时失败，继续使用{fallback}，5 分钟后重试")
            return False
