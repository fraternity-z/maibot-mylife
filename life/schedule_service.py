"""确定性的活动窗口；执行结果由实际检索和阅读服务记录。"""
from __future__ import annotations

from datetime import datetime
from typing import Any


def to_minute(text: str, *, allow_day_end: bool=False) -> int | None:
    try:
        if allow_day_end and text=="24:00":return 1440
        h,m=map(int,text.split(":"))
        if 0<=h<=23 and 0<=m<=59:return h*60+m
    except (ValueError,AttributeError):pass
    return None


def hhmm(value: int) -> str:
    value=max(0,min(1440,value)); return "24:00" if value==1440 else f"{value//60:02d}:{value%60:02d}"


class ScheduleService:
    def __init__(self, store: Any, config: Any, llm: Any, plugin_dir: str, logger: Any, bot_name: str="麦麦") -> None:
        self.store=store; self.config=config; self.bot_name=bot_name

    def update_config(self, config: Any) -> None:self.config=config

    def set_bot_name(self, name: str) -> None:
        if str(name or "").strip():self.bot_name=str(name).strip()

    async def ensure_day(self, now: datetime, personality: str="", weather_text: str="", force: bool=False,
                         memory_context: dict[str,Any]|None=None,
                         environment: dict[str,Any]|None=None) -> list[dict[str,Any]]:
        """旧接口参数仅兼容调用者；重建计划也不清除已经执行的记录。"""
        day=now.date().isoformat(); existing=await self.store.get_framework(day)
        if existing and all(str(node["id"]).startswith("task:") for node in existing):return existing
        windows=[(0,480,"sleep","夜间待机"),
                 (480,600,"leisure","待执行：检索新闻和主题资料"),
                 (600,720,"study","待执行：阅读小说并记录笔记"),
                 (720,840,"leisure","待执行：检索新闻和主题资料"),
                 (840,1080,"study","待执行：阅读小说并记录笔记"),
                 (1080,1410,"leisure","待执行：检索新闻和主题资料"),
                 (1410,1440,"sleep","夜间待机")]
        nodes=[{"id":f"task:{day}:{start}","day":day,"start_minute":start,"end_minute":end,
                "kind":kind,"summary":summary,"location":"在线","shareability":0.0}
               for start,end,kind,summary in windows]
        await self.store.replace_framework(day,nodes)
        return await self.store.get_framework(day)

    @staticmethod
    def current_and_next(nodes: list[dict[str,Any]], minute: int) -> tuple[dict[str,Any]|None,dict[str,Any]|None]:
        current=None; nxt=None
        for node in nodes:
            if node["start_minute"]<=minute<node["end_minute"]:current=node
            elif node["start_minute"]>minute:
                nxt=node; break
        return current,nxt

    async def record_activity(self, now: datetime, summary: str) -> None:
        """只由已完成的服务调用；不为计划、失败或下一时段编写场景。"""
        context=await self.context(now); current=context.get("current")
        clean=str(summary or "").strip()
        if not current or not clean or current["kind"]=="sleep":return
        old=str((context.get("scene") or {}).get("scene") or "")
        lines=old.splitlines()
        if clean not in lines:lines.append(clean)
        await self.store.save_scene(current["id"],"\n".join(lines)[-2000:],[])

    async def context(self, now: datetime) -> dict[str,Any]:
        nodes=await self.store.get_framework(now.date().isoformat())
        # 升级前随机生成的场景仍留在旧库，但不再作为当前活动事实。
        nodes=[node for node in nodes if str(node["id"]).startswith("task:")]
        current,nxt=self.current_and_next(nodes,now.hour*60+now.minute)
        scene=await self.store.get_scene(current["id"]) if current else {}
        return {"nodes":nodes,"current":current,"next":nxt,"scene":scene}
