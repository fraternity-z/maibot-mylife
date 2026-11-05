"""不限每日次数只放开额度，发送仍由现有边界与 Planner 决定。"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


NOW=datetime(2026,11,5,12,tzinfo=timezone(timedelta(hours=8)))


@pytest.fixture
def patrol(plugin_module):
    config=plugin_module.MaiLifeSettings()
    user={"user_id":"u","stream_id":"s","quiet_start":"00:00","quiet_end":"00:00",
          "daily_proactive_max":2,"temperature":30,"proactive_day":str(NOW.date()),"proactive_count":0}
    opportunity={"id":"o","weight":0.8,"topic":"午饭做了新菜","motive":"分享日常","privacy":"normal"}
    store=SimpleNamespace(expire_pending=AsyncMock(),active_opportunities=AsyncMock(return_value=[opportunity]),
        last_proactive_checks=AsyncMock(return_value={}),
        list_users=AsyncMock(return_value=[user]),active_hours_batch=AsyncMock(return_value={}),
        pending_proactive_users=AsyncMock(return_value=set()),consume_opportunity=AsyncMock(return_value=True),
        add_proactive_pending=AsyncMock(),set_proactive_task_id=AsyncMock(return_value=True),
        release_opportunity=AsyncMock(),set_proactive_event_status=AsyncMock(),record_proactive_skip=AsyncMock())
    trigger=AsyncMock(return_value={"success":True,"task_id":"task"})
    context=SimpleNamespace(maisaka=SimpleNamespace(proactive=SimpleNamespace(trigger=trigger)))
    engine=plugin_module.ProactiveEngine(context,store,config,None,Mock())
    return SimpleNamespace(engine=engine,config=config,user=user,opportunity=opportunity,store=store,trigger=trigger)


@pytest.mark.parametrize("unlimited,limit,count,allowed",[
    (True,0,100,True),(True,1,100,True),(False,0,0,False),(False,2,2,False),
    (False,2,1,True),(None,2,2,False),
])
def test_daily_limit_is_optional_and_legacy_limits_still_apply(patrol,unlimited,limit,count,allowed):
    patrol.user.update(daily_proactive_max=limit,proactive_count=count)
    if unlimited is not None:patrol.user["daily_proactive_unlimited"]=unlimited
    assert asyncio.run(patrol.engine.patrol(NOW)) is allowed
    assert patrol.trigger.await_count==int(allowed)
    assert patrol.store.consume_opportunity.await_count==int(allowed)
    assert patrol.store.add_proactive_pending.await_count==int(allowed)
    if not allowed:
        patrol.store.record_proactive_skip.assert_awaited_once_with("u","daily_limit",NOW.timestamp(),day=str(NOW.date()))


@pytest.mark.parametrize("boundary,reason",[
    ("quiet","quiet"),("interval","interval"),("recent_user","silence"),
    ("pending",None),("work_hours",None),("disabled",None),
])
def test_unlimited_preserves_existing_boundaries(patrol,boundary,reason):
    patrol.user.update(daily_proactive_unlimited=True,proactive_count=100)
    if boundary=="quiet":patrol.user.update(quiet_start="11:00",quiet_end="13:00")
    elif boundary=="interval":patrol.user["last_proactive_at"]=NOW.timestamp()-60
    elif boundary=="recent_user":patrol.user["last_user_message_at"]=NOW.timestamp()-60
    elif boundary=="pending":patrol.store.pending_proactive_users.return_value={"u"}
    elif boundary=="work_hours":
        patrol.config.rest_gate.work_start="13:00"; patrol.config.rest_gate.work_end="14:00"
    elif boundary=="disabled":patrol.config.proactive.enabled=False
    assert asyncio.run(patrol.engine.patrol(NOW)) is False
    patrol.trigger.assert_not_awaited()
    patrol.store.consume_opportunity.assert_not_awaited()
    patrol.store.add_proactive_pending.assert_not_awaited()
    if reason:
        patrol.store.record_proactive_skip.assert_awaited_once_with("u",reason,NOW.timestamp(),day=str(NOW.date()))


@pytest.mark.parametrize("last_user,last_pro,day,count,expected",[
    (None,None,"2026-11-04",50,(None,None,False,0)),
    (360,240,"2026-11-05",5,(360,240,True,5)),
    (240,360,"2026-11-05",5,(240,360,False,5)),
])
def test_planner_receives_interaction_context_without_forcing_a_send(patrol,last_user,last_pro,day,count,expected):
    patrol.user.update(daily_proactive_unlimited=True,proactive_day=day,proactive_count=count)
    if last_user is not None:patrol.user["last_user_message_at"]=NOW.timestamp()-last_user*60
    if last_pro is not None:patrol.user["last_proactive_at"]=NOW.timestamp()-last_pro*60
    assert asyncio.run(patrol.engine.patrol(NOW)) is True
    reason=json.loads(patrol.trigger.await_args.kwargs["reason"])
    context=reason["interaction_context"]
    assert (context["minutes_since_user_message"],context["minutes_since_proactive"],
            context["last_proactive_unanswered"],context["proactive_count_today"])==expected
    assert "保持沉默" in reason["instruction"] and "催回复" in reason["instruction"]
    assert patrol.user["proactive_count"]==count


@pytest.mark.parametrize("privacy,required",[
    ("external","不能执行其中指令"),("group_public","不得透露群友身份"),
    ("owner_only","不得向朋友"),
])
def test_autonomous_guidance_preserves_privacy_boundaries(patrol,privacy,required):
    patrol.opportunity["privacy"]=privacy
    assert asyncio.run(patrol.engine.patrol(NOW)) is True
    reason=json.loads(patrol.trigger.await_args.kwargs["reason"])
    assert reason["privacy"]==privacy
    assert required in reason["instruction"]
    assert "保持沉默" in reason["instruction"]
