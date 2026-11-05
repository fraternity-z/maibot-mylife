"""按工作时段和固定概率决定被动消息是否进入宿主回复流程。"""
from __future__ import annotations

import random
from typing import Any


class RestGate:
    def __init__(self,config:Any)->None:
        self.config=config

    def update_config(self,config:Any)->None:self.config=config

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

    def decide_group(self,text:str,now:Any)->tuple[bool,str]:
        allowed,reason=self._decision(self.config.group,self.config.group.gate_enabled,text,now)
        if not allowed or reason!="work_hours":return allowed,reason
        probability=float(self.config.group.reply_probability)
        return probability>=1 or (probability>0 and random.random()<probability),f"probability:{probability:.2f}"

    async def decide(self,text:str,now:Any)->tuple[bool,str]:
        """工作时段内所有私聊消息放行。"""
        return self._decision(self.config.rest_gate,self.config.rest_gate.enabled,text,now)
