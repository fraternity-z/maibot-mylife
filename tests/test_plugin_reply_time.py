"""消息 Hook 的概率、通知与校准时间接入回归。"""
import ast
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest


NOW=datetime(2026,11,5,15,30,tzinfo=timezone(timedelta(hours=8)))


@pytest.fixture
def plugin(plugin_module):
    instance=plugin_module.MaiLifePlugin()
    instance.config=plugin_module.MaiLifeSettings()
    instance.config.context.enabled=False
    instance.ctx=SimpleNamespace(logger=Mock())
    instance._get_logger=lambda:instance.ctx.logger
    for name in ("_store","_llm","_state","_schedule","_proactive","_continuity","_memory",
                 "_information","_group_observer","_relay","_bookshelf","_creation","_admin","_bedtime"):
        setattr(instance,name,AsyncMock())
    instance._store.get_user.return_value=None
    instance._store.get_sleep_runtime.return_value={"phase":"awake"}
    instance._store.proactive_for_task.return_value={}
    instance._env=Mock(now=Mock(return_value=NOW))
    instance._rest=plugin_module.RestGate(instance._store,instance.config,instance._llm,instance._state,instance.ctx.logger)
    instance._activate_planner_task=AsyncMock(return_value=None)
    instance._cancel_reply_confirmations=AsyncMock()
    instance._cancel_group_confirmations=AsyncMock()
    instance._supersede_active_task=AsyncMock()
    instance._relay_for_session=AsyncMock(return_value={})
    instance._spawn_transient=lambda coroutine,name:coroutine.close()
    return instance


def inbound(*,group=False,notify=False,text="普通消息",session="chat-1"):
    return {"platform":"qq","session_id":session,"message_id":"msg-1","is_notify":notify,
            "raw_message":[{"type":"text","data":text}],
            "message_info":{"user_info":{"user_id":"12345","user_nickname":"测试用户"},
                            "group_info":{"group_id":"67890","group_name":"测试群"} if group else None,
                            "additional_config":{"napcat_notice":"kept","custom":42}}}


@pytest.mark.parametrize("enabled",[False,True])
def test_probability_pass_preserves_message_and_existing_metadata(plugin_module,enabled):
    message=inbound()
    original=deepcopy(message)
    result=plugin_module.MaiLifePlugin._probability_pass({"message":message,"other":"kept"},message,enabled)
    changed=result["modified_kwargs"]["message"]
    assert result["action"]=="continue"
    assert result["modified_kwargs"]["other"]=="kept"
    assert changed["raw_message"]==original["raw_message"]
    assert changed["message_info"]["additional_config"]["custom"]==42
    if enabled:
        assert type(changed["message_info"]["additional_config"]["is_mentioned"]) is float
        assert changed["message_info"]["additional_config"]["is_mentioned"]==1.0
    else:
        assert "is_mentioned" not in changed["message_info"]["additional_config"]
    assert message==original


@pytest.mark.parametrize("notify",[False,True])
@pytest.mark.parametrize("probability",[0.0,1.0])
def test_group_messages_and_notifications_use_probability(plugin,notify,probability):
    plugin.config.group.reply_probability=probability
    message=inbound(group=True,notify=notify,text="紧急！醒醒！别回")
    result=asyncio.run(plugin.on_receive(message=message))
    assert result["action"]==("continue" if probability else "abort")
    if probability:
        assert result["modified_kwargs"]["message"]["message_info"]["additional_config"]["is_mentioned"]==1.0
    plugin._schedule.context.assert_not_awaited()


@pytest.mark.parametrize("configured",[False,True])
@pytest.mark.parametrize("session",["","chat-1"])
def test_outside_work_hours_block_configured_and_unknown_private_users(plugin,configured,session):
    plugin.config.rest_gate.work_start="16:00"
    plugin.config.rest_gate.work_end="18:00"
    if configured:
        plugin._store.get_user.return_value={"user_id":"12345","enabled":True,"stream_id":session,"display_name":"测试用户"}
    result=asyncio.run(plugin.on_receive(message=inbound(session=session)))
    assert result=={"action":"abort"}
    plugin._schedule.context.assert_not_awaited()
    assert not plugin._session_runtime


@pytest.mark.parametrize("group",[False,True])
def test_probability_sampled_only_once_for_admitted_message(plugin,group):
    plugin.config.group.reply_probability=0.5
    plugin._store.get_user.return_value={"user_id":"12345","enabled":True,"stream_id":"chat-1","display_name":"测试用户"}
    with patch("mai_integration_tests.life.rest_gate.random.random",return_value=0.2) as draw:
        result=asyncio.run(plugin.on_receive(message=inbound(group=group)))
    assert result["action"]=="continue"
    if group:draw.assert_called_once_with()
    else:draw.assert_not_called()
    plugin._schedule.context.assert_not_awaited()


@pytest.mark.parametrize("configured",[False,True])
@pytest.mark.parametrize("notify",[False,True])
def test_private_messages_always_pass_without_random_draw(plugin,configured,notify):
    if configured:
        plugin._store.get_user.return_value={"user_id":"12345","enabled":True,"stream_id":"chat-1","display_name":"测试用户"}
    with patch("mai_integration_tests.life.rest_gate.random.random",side_effect=AssertionError("private chat must not draw")) as draw:
        result=asyncio.run(plugin.on_receive(message=inbound(notify=notify,text="普通私聊")))
    assert result["action"]=="continue"
    assert result["modified_kwargs"]["message"]["message_info"]["additional_config"]["is_mentioned"]==1.0
    draw.assert_not_called()


@pytest.mark.parametrize("notify",[False,True])
@pytest.mark.parametrize("configured",[False,True])
def test_private_time_limit_disabled_still_forces_host_trigger(plugin,notify,configured):
    plugin.config.rest_gate.enabled=False
    plugin.config.rest_gate.work_start="01:00"
    plugin.config.rest_gate.work_end="02:00"
    if configured:
        plugin._store.get_user.return_value={"user_id":"12345","enabled":True,"stream_id":"chat-1","display_name":"测试用户"}
    message=inbound(notify=notify)
    original_additional=message["message_info"]["additional_config"]
    original_additional.update({"is_mentioned":False,"at_bot":False,"source":"adapter"})
    with patch("mai_integration_tests.life.rest_gate.random.random",side_effect=AssertionError("private must not draw")):
        result=asyncio.run(plugin.on_receive(message=message))
    additional=result["modified_kwargs"]["message"]["message_info"]["additional_config"]
    assert result["action"]=="continue"
    assert type(additional["is_mentioned"]) is float and additional["is_mentioned"]==1.0
    assert additional["at_bot"] is False and additional["source"]=="adapter" and additional["custom"]==42
    assert original_additional["is_mentioned"] is False


def test_private_time_limit_disabled_keeps_planner_reply_instruction(plugin):
    plugin.config.rest_gate.enabled=False
    result=asyncio.run(plugin.on_planner(session_id="chat-1",messages=[{"role":"system","content":"人设"}]))
    assert "正常回应" in str(result["modified_kwargs"]["messages"])


@pytest.mark.parametrize("group",[False,True])
@pytest.mark.parametrize("text",["/麦麦状态","/other-command"])
def test_commands_bypass_work_hours_and_probability(plugin,group,text):
    plugin.config.group.reply_probability=0
    plugin.config.rest_gate.work_start=plugin.config.group.work_start="01:00"
    plugin.config.rest_gate.work_end=plugin.config.group.work_end="02:00"
    message=inbound(group=group,text=text)
    result=asyncio.run(plugin.on_receive(message=message))
    assert result["action"]=="continue"
    assert "is_mentioned" not in message["message_info"]["additional_config"]


@pytest.mark.parametrize("group",[False,True])
@pytest.mark.parametrize("notify",[False,True])
def test_work_hours_apply_to_messages_and_notifications(plugin,group,notify):
    plugin.config.group.reply_probability=1
    plugin.config.rest_gate.work_start=plugin.config.group.work_start="22:00"
    plugin.config.rest_gate.work_end=plugin.config.group.work_end="07:00"
    assert asyncio.run(plugin.on_receive(message=inbound(group=group,notify=notify)))["action"]=="abort"


@pytest.mark.parametrize("group",[False,True])
@pytest.mark.parametrize("legacy",[False,True])
def test_planner_gets_calibrated_time_without_life_context(plugin,group,legacy):
    if group:plugin._group_sessions.add("chat-1")
    old_time="时间：2026-11-05 07:30:00"
    payload={"messages":[{"role":"system","content":"宿主人设"},{"role":"user","content":old_time}]} if legacy else {
        "items":[{"item_type":"SystemMessageItem","parts":[{"type":"text","text":"宿主人设"}]},
                 {"item_type":"UserMessageItem","parts":[{"type":"text","text":old_time}]}]}
    result=asyncio.run(plugin.on_planner(session_id="chat-1",**payload))
    changed=result["modified_kwargs"]
    rendered=str(changed.get("items",changed.get("messages")))
    assert "2026-11-05" in rendered and "15:30" in rendered
    assert "Asia/Shanghai" in rendered and "+08:00" in rendered
    assert "宿主人设" in rendered
    assert "当前时间基准" in rendered


@pytest.mark.parametrize("group",[False,True])
def test_replyer_gets_calibrated_time_without_life_context(plugin,group):
    if group:plugin._group_sessions.add("chat-1")
    result=asyncio.run(plugin.on_replyer(session_id="chat-1",reply_message_id="msg-1",extra_prompt="其它插件要求"))
    prompt=result["modified_kwargs"]["extra_prompt"]
    assert "2026-11-05" in prompt and "15:30" in prompt
    assert "Asia/Shanghai" in prompt and "+08:00" in prompt
    assert "其它插件要求" in prompt
    assert "当前时间基准" in prompt


@pytest.mark.parametrize("group",[False,True])
@pytest.mark.parametrize("hour,minute,expected",[(15,59,"continue"),(16,0,"abort")])
def test_pending_reply_cannot_cross_work_end(plugin,group,hour,minute,expected):
    plugin.config.rest_gate.work_start=plugin.config.group.work_start="09:00"
    plugin.config.rest_gate.work_end=plugin.config.group.work_end="16:00"
    if group:plugin._group_sessions.add("chat-1")
    plugin._reply_confirmations[("chat-1","msg-1")]={
        "anchor":"msg-1","turn_anchor":"msg-1","task_id":"","created_at":NOW.timestamp(),
        "turn_scope":"group:67890:12345" if group else "chat-1",
    }
    plugin._env.now.return_value=NOW.replace(hour=hour,minute=minute)
    with patch("mai_integration_tests.life.rest_gate.random.random",side_effect=AssertionError("send must not redraw")):
        result=asyncio.run(plugin.on_send_before(message=inbound(group=group),reply_message_id="msg-1"))
    assert result["action"]==expected


@pytest.mark.parametrize("group",[False,True])
def test_command_response_without_passive_confirmation_can_send_outside_hours(plugin,group):
    plugin.config.rest_gate.work_start=plugin.config.group.work_start="09:00"
    plugin.config.rest_gate.work_end=plugin.config.group.work_end="15:00"
    if group:plugin._group_sessions.add("chat-1")
    result=asyncio.run(plugin.on_send_before(message=inbound(group=group,text="管理员命令的返回结果"),reply_message_id="command-1"))
    assert result["action"]=="continue"


def test_send_rechecks_time_after_pending_task_lookup(plugin):
    plugin.config.rest_gate.work_start="09:00"
    plugin.config.rest_gate.work_end="16:00"
    plugin._env.now.return_value=NOW.replace(hour=15,minute=59,second=59)
    plugin._reply_confirmations[("chat-1","msg-1")]={
        "anchor":"msg-1","turn_anchor":"msg-1","task_id":"","created_at":NOW.timestamp(),"turn_scope":"chat-1",
    }
    async def lookup(*args):
        plugin._env.now.return_value=NOW.replace(hour=16,minute=0)
        return {}
    plugin._relay_for_session.side_effect=lookup
    assert asyncio.run(plugin.on_send_before(message=inbound(),reply_message_id="msg-1"))["action"]=="abort"


@pytest.mark.parametrize("group,notify,turn_enabled",[
    (False,False,True),(False,True,True),(True,False,False),(True,True,True),
])
def test_replyer_origin_covers_messages_without_life_confirmation(plugin,group,notify,turn_enabled):
    plugin.config.group.reply_probability=1
    plugin.config.group.turn_enabled=turn_enabled
    policy=plugin.config.group if group else plugin.config.rest_gate
    policy.work_start="09:00"
    policy.work_end="16:00"
    plugin._user_by_session=AsyncMock(return_value={})
    message=inbound(group=group,notify=notify)
    async def flow():
        assert (await plugin.on_receive(message=message))["action"]=="continue"
        await plugin.on_replyer_after(session_id="chat-1",reply_message_id="msg-1",response="正常回应")
        assert not plugin._reply_confirmations
        plugin._env.now.return_value=NOW.replace(hour=16,minute=0)
        return await plugin.on_send_before(message=message,reply_message_id="msg-1")
    assert asyncio.run(flow())["action"]=="abort"


def test_later_segment_still_obeys_hours_after_first_send_consumes_confirmation(plugin):
    plugin.config.rest_gate.work_start="09:00"
    plugin.config.rest_gate.work_end="16:00"
    user={"user_id":"12345","enabled":True,"stream_id":"chat-1","display_name":"测试用户","role":"owner"}
    plugin._store.get_user.return_value=user
    plugin._user_by_session=AsyncMock(return_value=user)
    plugin._store.reserve_reply_turn.return_value=True
    plugin._store.pop_wake_candidate.return_value={}
    plugin._record_mood_event=AsyncMock()
    message=inbound()
    async def flow():
        await plugin.on_receive(message=message)
        await plugin.on_replyer_after(session_id="chat-1",reply_message_id="msg-1",response="第一段。第二段。")
        assert ("chat-1","msg-1") in plugin._reply_confirmations
        assert (await plugin.on_send_before(message=message,reply_message_id="msg-1"))["action"]=="continue"
        await plugin.on_send_after(message=message,reply_message_id="msg-1",sent=True)
        assert not plugin._reply_confirmations
        plugin._env.now.return_value=NOW.replace(hour=16,minute=0)
        return await plugin.on_send_before(message=message,reply_message_id="msg-1")
    assert asyncio.run(flow())["action"]=="abort"


def test_config_reload_retains_group_reply_origin_and_applies_new_hours(plugin):
    plugin.config.group.reply_probability=1
    plugin.config.group.turn_enabled=False
    plugin._user_by_session=AsyncMock(return_value={})
    plugin._stop_tasks=AsyncMock()
    plugin._maintenance_tick=AsyncMock()
    plugin._start_tasks=Mock()
    plugin._resolve_all_streams=AsyncMock()
    for name in ("_llm","_state","_schedule","_proactive","_continuity","_memory","_information",
                 "_group_observer","_relay","_bookshelf","_creation","_admin","_bedtime"):
        getattr(plugin,name).update_config=Mock()
    message=inbound(group=True)
    async def flow():
        await plugin.on_receive(message=message)
        await plugin.on_replyer_after(session_id="chat-1",reply_message_id="msg-1",response="群回复")
        plugin.config.group.work_start="09:00"
        plugin.config.group.work_end="15:00"
        await plugin.on_config_update("self",{},"updated")
        assert "chat-1" in plugin._group_sessions
        return await plugin.on_send_before(message=message,reply_message_id="msg-1")
    assert asyncio.run(flow())["action"]=="abort"


@pytest.mark.parametrize("hour,minute,checked_opportunities",[(15,59,True),(16,0,False)])
def test_proactive_patrol_uses_private_work_window_without_random_draw(plugin_module,hour,minute,checked_opportunities):
    config=plugin_module.MaiLifeSettings()
    config.proactive.enabled=True
    config.rest_gate.work_start="09:00"
    config.rest_gate.work_end="16:00"
    store=AsyncMock()
    store.active_opportunities.return_value=[]
    engine=plugin_module.ProactiveEngine(Mock(),store,config,Mock(),Mock())
    with patch("mai_integration_tests.life.rest_gate.random.random",side_effect=AssertionError("proactive must not redraw")):
        result=asyncio.run(engine.patrol(NOW.replace(hour=hour,minute=minute),{"sleep_phase":"awake","energy":100}))
    assert result is False
    assert store.active_opportunities.await_count==int(checked_opportunities)


def _host_function(path,name,namespace):
    tree=ast.parse(path.read_text(encoding="utf-8"))
    node=next(item for item in ast.walk(tree) if isinstance(item,ast.FunctionDef) and item.name==name)
    node.decorator_list=[]
    namespace["__name__"]="isolated_host_contract"
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),"exec",flags=__import__("__future__").annotations.compiler_flag),namespace)
    return namespace[name]


def test_probability_marker_reaches_real_host_forced_trigger(plugin_module):
    root=Path(__file__).resolve().parents[1]
    candidates=[Path(os.environ.get("MAIBOT_SOURCE_DIR","__missing_host__")),root.parent/"hhh"/"MaiBot",root.parent/"MaiBot"]
    host=next((path for path in candidates if (path/"src/chat/utils/utils.py").is_file()),None)
    if host is None:pytest.skip("设置 MAIBOT_SOURCE_DIR 可检查宿主源码契约")
    timing=SimpleNamespace(inevitable_at_reply=False,mentioned_bot_reply=False)
    config=SimpleNamespace(bot=SimpleNamespace(nickname="麦麦",alias_names=[]),
                           chat=SimpleNamespace(reply_timing=timing))
    detect=_host_function(host/"src/chat/utils/utils.py","is_mentioned_bot_in_message",{
        "re":re,"get_bot_accounts":lambda platform:[],"_has_at_component_targeting_bot":lambda *args:False,
        "global_config":config,
    })
    update=_host_function(host/"src/maisaka/runtime.py","_update_message_trigger_state",{
        "is_mentioned_bot_in_message":detect,"global_config":config,
    })
    message=inbound()
    result=plugin_module.MaiLifePlugin._probability_pass({"message":message},message,True)
    info=result["modified_kwargs"]["message"]["message_info"]
    host_message=SimpleNamespace(processed_plain_text="普通消息",platform="qq",is_at=False,is_mentioned=False,
                                 message_id="msg-1",message_info=SimpleNamespace(**info))
    runtime=SimpleNamespace(_arm_forced_turn=Mock())
    update(runtime,host_message)
    runtime._arm_forced_turn.assert_called_once_with(host_message,is_at=False,is_mentioned=True)
