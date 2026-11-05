"""优先读取内置新闻订阅，失败时使用宿主搜索，并按需抓取正文。"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from itertools import zip_longest
from typing import Any
from urllib.parse import urljoin
from xml.etree import ElementTree

from .host_search import HostSearchService
from .http_client import HttpClient, HttpRequestError


NEWS_SOURCES={
    "ithome":("IT之家","https://www.ithome.com/rss/"),
    "solidot":("Solidot","https://www.solidot.org/index.rss"),
    "sspai":("少数派","https://sspai.com/feed"),
}


def _published_timestamp(value:Any)->float|None:
    if not value:return None
    try:
        stamp=float(value)
    except (TypeError,ValueError):
        try:
            try:parsed=datetime.fromisoformat(str(value).strip().replace("Z","+00:00"))
            except ValueError:parsed=parsedate_to_datetime(str(value))
            stamp=parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
        except (ValueError,TypeError,OverflowError,OSError):return None
    return stamp if math.isfinite(stamp) else None


def _plain_text(value:str)->str:
    value=re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>"," ",value,flags=re.I|re.S)
    return " ".join(html.unescape(re.sub(r"<[^>]+>"," ",value)).split())


class NewsService:
    def __init__(self,store:Any,config:Any,search:HostSearchService,logger:Any,llm:Any,bot_name:str="麦麦")->None:
        self.store=store; self.config=config; self.search=search; self.logger=logger
        self.llm=llm; self.bot_name=bot_name
        self.http=HttpClient(logger); self._lock=asyncio.Lock()

    def update_config(self,config:Any)->None:self.config=config

    def set_bot_name(self,name:str)->None:
        clean=str(name or "").strip()
        if clean:self.bot_name=clean

    def sources(self)->list[dict[str,str]]:
        return [{"id":key,"name":NEWS_SOURCES[key][0],"url":NEWS_SOURCES[key][1]}
                for key in dict.fromkeys(getattr(self.config.network,"news_sources",[])) if key in NEWS_SOURCES]

    @staticmethod
    def _day_bounds(now:Any)->tuple[float,float]:
        start=now.replace(hour=0,minute=0,second=0,microsecond=0)
        return start.timestamp(),(start+timedelta(days=1)).timestamp()

    async def refresh_due(self,now:Any,schedule:dict[str,Any],personality:str)->int:
        """后台和手动刷新共用额度，避免并发重复读取。"""
        async with self._lock:
            return await self._refresh_due(now,schedule,personality)

    async def _refresh_due(self,now:Any,schedule:dict[str,Any],personality:str)->int:
        cfg=self.config.network
        if not cfg.enabled or not cfg.news_enabled or int(cfg.news_daily_max)<=0:return 0
        current=schedule.get("current") or {}
        if str(current.get("kind") or "") not in set(cfg.news_allowed_schedule_types):return 0
        start,end=self._day_bounds(now)
        if await self.store.search_attempt_count("news",start,end)>=int(cfg.news_daily_max):return 0
        sources=self.sources(); started=time.perf_counter()
        results=await self._feed_results(sources,now.timestamp()) if sources else []
        query=""
        if not results and self.search.available():
            query=await self.search.sanitize_query(await self._plan_query(now,personality,schedule))
        if query:
            response=await self.search.search(query,operation="news",max_results=int(cfg.max_results))
        elif sources:
            await self.search.record(
                "news","内置新闻源："+"、".join(source["name"] for source in sources),"rss",
                bool(results),"" if results else "feed_unavailable",(time.perf_counter()-started)*1000,
                len(results),results,now.timestamp(),
            )
            response={"results":results,"service":"rss"}
        else:return 0
        results=self._recent_results(response["results"],now.timestamp())
        if not results:
            if response["results"]:
                self.logger.info("[MaiLife] 新闻搜索结果全部早于 3 天，未写入新闻库")
            return 0
        results=results[:min(5,int(self.config.network.max_results))]
        contents=await self._read_articles(results)
        changed=0; now_ts=now.timestamp(); retention=int(cfg.news_retention_days)*86400
        service=str(response.get("service") or "host")
        for index,result in enumerate(results):
            uncited=not result["url"]
            summary=result["snippet"]
            if uncited:summary="宿主结果、无外部链接："+summary
            # 去重键只由 url 或 title+snippet 决定：同一条新闻不会因服务名变化而重复入库。
            stable=result["url"] or hashlib.sha256(f"{result['title']}:{result['snippet']}".encode()).hexdigest()
            item_id=hashlib.sha256(stable.encode()).hexdigest()
            content=contents.get(index,"")[:int(cfg.news_max_article_chars)]
            digest=hashlib.sha256(json.dumps([result["title"],summary,content],ensure_ascii=False).encode()).hexdigest()
            source_id=result.get("source_id") or f"host:{service}"+(":uncited" if uncited else "")
            if await self.store.upsert_news_item({
                "id":item_id,"source_id":source_id,"title":result["title"],"url":result["url"],
                "summary":summary,"content":content,
                "published_at":_published_timestamp(result.get("published_at")) or now_ts,"fetched_at":now_ts,
                "content_hash":digest,"expires_at":now_ts+retention,
            }):changed+=1
        return changed

    @staticmethod
    def _recent_results(rows:list[dict[str,Any]],now_ts:float)->list[dict[str,Any]]:
        """新闻只要近 3 天的；兼容 RSS、Atom 和宿主时间格式，无日期时保留。"""
        cutoff=now_ts-3*86400
        return [row for row in rows if (stamp:=_published_timestamp(row.get("published_at"))) is None or stamp>=cutoff]

    async def _feed_results(self,sources:list[dict[str,str]],now_ts:float)->list[dict[str,Any]]:
        semaphore=asyncio.Semaphore(int(self.config.network.news_max_concurrency))
        async def read(source:dict[str,str])->list[dict[str,Any]]:
            try:
                async with semaphore:
                    response=await self.http.get(source["url"],timeout=8,max_bytes=1_000_000,public_only=True)
                return self._recent_results(self._parse_feed(response.body,response.url,source["id"]),now_ts)
            except Exception:
                self.logger.warning(f"[MaiLife] 新闻源 {source['name']} 读取失败，已跳过")
                return []
        batches=await asyncio.gather(*(read(source) for source in sources))
        results=[]; seen=set(); limit=min(5,int(self.config.network.max_results))
        # 各源交替取条目，避免第一家覆盖所有阅读名额。
        for group in zip_longest(*batches):
            for row in group:
                if row is None or row["url"] in seen:continue
                seen.add(row["url"]); results.append(row)
                if len(results)>=limit:return results
        return results

    @staticmethod
    def _parse_feed(body:bytes,base_url:str,source_id:str)->list[dict[str,Any]]:
        if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b",body.replace(b"\x00",b""),re.I):
            raise ValueError("新闻订阅不允许 DTD 或实体声明")
        root=ElementTree.fromstring(body)
        entries=root.findall("./channel/item") if root.tag=="rss" else root.findall("{*}entry")
        rows=[]
        for entry in entries[:20]:
            def field(*names:str)->str:
                for name in names:
                    node=entry.find("{*}"+name)
                    if node is not None:return "".join(node.itertext()).strip()
                return ""
            link=next((node.get("href") or "" for node in entry.findall("{*}link")
                       if node.get("rel","alternate")=="alternate" and node.get("href")),"") or field("link")
            title=_plain_text(field("title"))[:240]
            if not title or not link:continue
            try:
                url=HttpClient.validate_url(urljoin(base_url,link))
                if HttpClient.private_host_reason(url):continue
            except (HttpRequestError,ValueError):continue
            rows.append({"title":title,"url":url,"snippet":_plain_text(field("description","summary","content"))[:1200],
                         "published_at":field("pubDate","published","updated","date"),"source_id":f"rss:{source_id}"})
        return rows

    async def _plan_query(self,now,personality:str,schedule:dict)->str:
        """模型可选；规划失败仍用当天的明确新闻查询，结果必须由宿主实际检索。"""
        fallback=f"今日新闻 {now.date().isoformat()}"
        if not self.llm.task_available("news"):return fallback
        current=schedule.get("current") or {}
        try:
            result=await self.llm.generate_json(
                f"你是{self.bot_name}。根据你的人设与当前状态，自主决定此刻最想了解的一类新闻，给出一个简短、"
                "适合网页搜索的查询词（不超过 40 字）。不要搜索用户身份、账号、群名、私密关系或聊天原句。\n"
                + json.dumps({
                    "personality": personality[:1200],
                    "current_activity": current.get("summary") or "",
                }, ensure_ascii=False)
                + "\n只返回JSON：{\"query\":\"\"}。",
                f"你自主决定{self.bot_name}此刻读什么新闻。", {"query":fallback}, max_tokens=0,
                task_kind="news", request_type="news_query_planning",
            )
        except Exception:
            self.logger.warning("[MaiLife] 新闻查询规划失败，改用当日新闻查询")
            return fallback
        query=str(result.get("query") or "") if isinstance(result,dict) else ""
        return " ".join(query.split())[:100] or fallback

    async def _read_articles(self,results:list[dict[str,Any]])->dict[int,str]:
        """并发经宿主 web.fetch 读取最多三篇正文；任一页面失败只降级为搜索摘要。"""
        limit=min(len(results),int(self.config.network.news_full_text_count),3)
        if limit<=0:return {}
        semaphore=asyncio.Semaphore(int(self.config.network.news_max_concurrency))
        async def read(index:int,result:dict[str,Any])->tuple[int,str]:
            if not result["url"]:return index,""
            async with semaphore:
                return index,await self.search.fetch_article(result["url"])
        values=await asyncio.gather(*(read(index,result) for index,result in enumerate(results[:limit])))
        max_chars=int(self.config.network.news_max_article_chars)
        return {index:content[:max_chars] for index,content in values if content}
