"""按工作时段和固定概率决定被动消息是否进入宿主回复流程。"""
from __future__ import annotations

import random
from typing import Any


class RestGate:
    def __init__(self,store:Any,config:Any,llm:Any,state_engine:Any,logger:Any,bot_name:str="麦麦")->None:
        self.store=store; self.config=config; self.state_engine=state_engine; self.logger=logger
        self.bot_name=bot_name

    def update_config(self,config:Any)->None:self.config=config

    def set_bot_name(self,name:str)->None:
        clean=str(name or "").strip()
        if clean:self.bot_name=clean

    @staticmethod
    def in_work_window(start:str,end:str,current:str)->bool:
        """开始时刻包含、结束时刻不包含；相同表示全天，支持跨午夜。"""
        if start==end:return True
        return start<=current<end if start<end else current>=start or current<end

    @classmethod
    def _decision(cls,cfg:Any,enabled:bool,text:str,now:Any)->tuple[bool,str]:
        if not enabled:return True,"disabled"
        if str(text or "").lstrip().startswith("/"):return True,"command"
        if not cls.in_work_window(cfg.work_start,cfg.work_end,now.strftime("%H:%M")):
            return False,"outside_work_hours"
        return True,"work_hours"

    def decide_group(self,text:str,now:Any,group_id:str,segment:dict[str,Any]|None=None)->tuple[bool,str]:
        allowed,reason=self._decision(self.config.group,self.config.group.gate_enabled,text,now)
        if not allowed or reason!="work_hours":return allowed,reason
        probability=float(self.config.group.reply_probability)
        return probability>=1 or (probability>0 and random.random()<probability),f"probability:{probability:.2f}"

    async def _candidate(self,user_id:str,session_id:str,message_id:str,reason:str,now:Any)->None:
        if session_id:
            lifetime=max(900,int(self.config.context.turn_expire_seconds)*3)
            await self.store.set_wake_candidate(
                session_id,user_id,message_id,reason,now.timestamp(),now.timestamp()+lifetime,
            )

    async def decide(self,user_id:str,text:str,now:Any,segment:dict[str,Any]|None,
                     *,session_id:str="",message_id:str="",media:list[str]|None=None,
                     allow_wake:bool=True)->tuple[bool,str]:
        """工作时段内所有私聊消息放行；睡眠状态只用于成功发送后的生活结算。"""
        allowed,reason=self._decision(self.config.rest_gate,self.config.rest_gate.enabled,text,now)
        if allowed and reason!="command" and session_id and allow_wake:
            runtime=await self.store.get_sleep_runtime()
            if str(runtime.get("phase") or "") in {"falling_asleep","light_sleep","deep_sleep"}:
                await self._candidate(user_id,session_id,message_id,reason,now)
        return allowed,reason

    async def commit_for_send(self,session_id:str,now:Any,message_id:str="")->bool:
        """成功发送后才结算醒来；已清醒时不重复消耗精力。"""
        candidate=await self.store.pop_wake_candidate(session_id,now.timestamp(),message_id)
        if not candidate:return False
        runtime=await self.store.get_sleep_runtime()
        if str(runtime.get("phase") or "") not in {"falling_asleep","light_sleep","deep_sleep"}:return False
        await self.state_engine.mark_woken(now,str(candidate.get("reason") or "回复后醒来"))
        return True
