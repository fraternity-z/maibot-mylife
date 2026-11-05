"""离线验证工作时段、固定概率、旧配置迁移及睡眠结算。"""
import asyncio
from datetime import datetime, timedelta, timezone
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError


@pytest.fixture
def modules(plugin_module):
    names=("config","life.rest_gate","life.bedtime","life.life_state","social.group_observer","core.llm_service")
    return SimpleNamespace(**{name.rsplit(".",1)[-1]:importlib.import_module(f"mai_integration_tests.{name}") for name in names})


@pytest.fixture
def setup_gate(modules):
    config=modules.config.MaiLifeSettings()
    store=SimpleNamespace(get_sleep_runtime=AsyncMock(return_value={"phase":"awake"}),
                          set_wake_candidate=AsyncMock(),pop_wake_candidate=AsyncMock(return_value={}))
    engine=SimpleNamespace(mark_woken=AsyncMock())
    gate=modules.rest_gate.RestGate(store,config,Mock(),engine,Mock())
    now=datetime(2026,11,5,12,0,tzinfo=timezone(timedelta(hours=8)))
    return gate,config,store,engine,now


@pytest.mark.parametrize("start,end,current,expected",[
    ("09:00","18:00","08:59",False),("09:00","18:00","09:00",True),
    ("09:00","18:00","17:59",True),("09:00","18:00","18:00",False),
    ("22:00","06:00","22:00",True),("22:00","06:00","05:59",True),
    ("22:00","06:00","06:00",False),("22:00","06:00","12:00",False),
    ("00:00","00:00","23:59",True),("09:00","09:00","00:00",True),
])
def test_work_window(modules,start,end,current,expected):
    assert modules.rest_gate.RestGate.in_work_window(start,end,current) is expected


@pytest.mark.parametrize("probability,sample,expected",[(0,0,False),(1,0.999999,True),(0.18,0.18,False),(0.18,0.179,True)])
@pytest.mark.parametrize("text,media",[("普通消息",[]),("救命，醒醒",[]),("别回，继续睡",[]),("",["image","emoji"])])
def test_content_does_not_override_probability(setup_gate,monkeypatch,modules,probability,sample,expected,text,media):
    gate,config,store,engine,now=setup_gate
    config.group.reply_probability=probability
    monkeypatch.setattr(modules.rest_gate.random,"random",lambda:sample)
    assert asyncio.run(gate.decide("123",text,now,{"kind":"sleep"},session_id="s",media=media,allow_wake=False))[0] is True
    assert gate.decide_group(text,now,"g",{"kind":"sleep"})[0] is expected
    store.set_wake_candidate.assert_not_awaited()
    engine.mark_woken.assert_not_awaited()


def test_private_messages_never_roll_probability(setup_gate,monkeypatch,modules):
    gate,config,store,engine,now=setup_gate
    random=Mock(side_effect=AssertionError("private messages must always pass"))
    monkeypatch.setattr(modules.rest_gate.random,"random",random)
    for text in ("消息","醒醒","别回",""):
        assert asyncio.run(gate.decide("123",text,now,{"kind":"sleep"},media=["image"],allow_wake=False))[0]
    random.assert_not_called()


def test_work_hours_override_probability_but_commands_pass(setup_gate,monkeypatch,modules):
    gate,config,store,engine,now=setup_gate
    for cfg in (config.rest_gate,config.group):
        cfg.work_start="13:00"; cfg.work_end="18:00"
    config.group.reply_probability=1
    random=Mock(side_effect=AssertionError("outside hours must not sample"))
    monkeypatch.setattr(modules.rest_gate.random,"random",random)
    assert asyncio.run(gate.decide("123","醒醒",now,None))[0] is False
    assert gate.decide_group("醒醒",now,"g")[0] is False
    assert asyncio.run(gate.decide("123","/麦麦帮助",now,None))[0] is True
    assert gate.decide_group("/麦麦帮助",now,"g")[0] is True
    store.get_sleep_runtime.assert_not_awaited()


def test_disabled_policy_and_hot_update(setup_gate,modules):
    gate,config,store,engine,now=setup_gate
    replacement=modules.config.MaiLifeSettings(rest_gate={"enabled":False,"work_start":"13:00","work_end":"18:00"},
                                               group={"gate_enabled":False,"reply_probability":0})
    gate.update_config(replacement)
    assert asyncio.run(gate.decide("123","消息",now,None))[0] is True
    assert gate.decide_group("消息",now,"g")[0] is True
    replacement.rest_gate.enabled=True; replacement.group.gate_enabled=True
    assert asyncio.run(gate.decide("123","消息",now,None))[0] is False
    assert gate.decide_group("消息",now,"g")[0] is False


def test_sleep_only_changes_after_successful_send(setup_gate):
    gate,config,store,engine,now=setup_gate
    store.get_sleep_runtime.return_value={"phase":"deep_sleep"}
    assert asyncio.run(gate.decide("123","消息",now,None,session_id="s",message_id="m"))[0]
    store.set_wake_candidate.assert_awaited_once()
    engine.mark_woken.assert_not_awaited()
    assert not asyncio.run(gate.commit_for_send("s",now,"other"))
    store.pop_wake_candidate.return_value={"reason":"probability:1.00"}
    assert asyncio.run(gate.commit_for_send("s",now,"m"))
    engine.mark_woken.assert_awaited_once_with(now,"probability:1.00")
    store.get_sleep_runtime.return_value={"phase":"woken"}
    assert not asyncio.run(gate.commit_for_send("s",now,"m2"))
    assert engine.mark_woken.await_count==1


@pytest.mark.parametrize("section",["rest_gate","group"])
@pytest.mark.parametrize("field,value",[("work_start","9:00"),("work_end","24:00"),("work_start","12:60"),("work_end",None)])
def test_invalid_config_rejected(modules,section,field,value):
    with pytest.raises(ValidationError):modules.config.MaiLifeSettings(**{section:{field:value}})


@pytest.mark.parametrize("value",[-0.01,1.01])
def test_invalid_group_probability_rejected(modules,value):
    with pytest.raises(ValidationError):modules.config.GroupChatSettings(reply_probability=value)


def test_legacy_enabled_config_migrates_hours_probability_and_grace(modules):
    config=modules.config.MaiLifeSettings(
        rest_gate={"enabled":True,"mode":"llm","wake_probability":0.4,"night_start":"23:00","night_end":"07:00", "awake_grace_minutes":45},
        group={"gate_enabled":True,"gate_mode":"time_window","night_start":"00:30","night_end":"08:00"})
    assert (config.rest_gate.work_start,config.rest_gate.work_end)==("07:00","23:00")
    assert "reply_probability" not in config.rest_gate.model_dump()
    assert (config.group.work_start,config.group.work_end,config.group.reply_probability)==("08:00","00:30",0.18)
    assert config.state.awake_grace_minutes==45
    assert "mode" not in config.rest_gate.model_dump()
    assert "force_wake_terms" not in config.group.model_dump()
    assert "rest_wakeup_task" not in config.models.model_dump()


def test_legacy_disabled_and_schedule_modes(modules):
    config=modules.config.MaiLifeSettings(rest_gate={"enabled":False,"wake_probability":0.18,"gate_mode":"time_window"},
                                        group={"gate_enabled":False,"gate_mode":"schedule"})
    assert config.rest_gate.enabled
    assert config.group.gate_enabled and config.group.reply_probability==0.18
    assert config.rest_gate.work_start==config.rest_gate.work_end=="00:00"
    schedule=modules.config.RestGateSettings(enabled=True,gate_mode="schedule",wake_probability=0.3)
    assert schedule.work_start==schedule.work_end=="00:00"
    assert "reply_probability" not in schedule.model_dump()


def test_explicit_new_settings_win_over_old_fields(modules):
    config=modules.config.MaiLifeSettings(rest_gate={"enabled":False,"work_start":"09:00","wake_probability":0.2,
                                                  "night_start":"21:00","awake_grace_minutes":50},
                                        state={"awake_grace_minutes":10})
    assert not config.rest_gate.enabled
    assert config.rest_gate.work_start=="09:00"
    assert config.state.awake_grace_minutes==10


def test_group_observation_uses_window_without_random(setup_gate,modules,monkeypatch):
    gate,config,store,engine,now=setup_gate
    config.group.work_start="13:00"; config.group.work_end="18:00"
    observer=modules.group_observer.GroupObserver(store,config,Mock(),Mock())
    monkeypatch.setattr(modules.rest_gate.random,"random",Mock(side_effect=AssertionError("second sample")))
    assert asyncio.run(observer._group_quiet_now(now))
    config.group.work_start="12:00"
    assert not asyncio.run(observer._group_quiet_now(now))


def test_bedtime_follows_schedule_independently_of_reply_settings(setup_gate,modules):
    gate,config,store,engine,now=setup_gate
    segment={"start_minute":22*60,"end_minute":6*60,"kind":"sleep"}
    store.get_framework=AsyncMock(return_value=[segment])
    store.get_sleep_runtime.return_value={"phase":"woken","awake_grace_until":0}
    engine.mark_resleep=AsyncMock()
    bedtime=modules.bedtime.BedtimeManager(None,store,config,None,engine,Mock())
    morning=now.replace(hour=2)
    asyncio.run(bedtime.tick(morning))
    assert bedtime.approaching(morning)
    assert bedtime._segment_start_ts(morning,segment)==morning.replace(hour=22).timestamp()-86400
    engine.mark_resleep.assert_awaited_once_with(morning)


def test_mark_woken_does_not_repeat_energy_cost(setup_gate,modules):
    gate,config,store,unused,now=setup_gate
    runtime={"phase":"deep_sleep","woken_count":0}
    state={"energy":70}
    store.get_sleep_runtime.return_value=runtime
    store.get_state=AsyncMock(return_value=state)
    store.save_sleep_runtime=AsyncMock(); store.save_state=AsyncMock()
    engine=modules.life_state.LifeStateEngine(store,config,Mock(),Mock())
    asyncio.run(engine.mark_woken(now,"回复")); asyncio.run(engine.mark_woken(now,"又回复"))
    assert state["energy"]==67 and runtime["woken_count"]==1
    assert runtime["awake_grace_until"]==now.timestamp()+30*60


def test_llm_usage_and_daily_counts_use_injected_time(setup_gate,modules):
    gate,config,store,engine,now=setup_gate
    store.record_llm_usage=AsyncMock()
    service=modules.llm_service.LLMService(SimpleNamespace(logger=Mock()),config,store,now=lambda:now)
    service._count_json_failure("schedule")
    assert service.json_failure_snapshot()["day"]=="2026-11-05"
    assert "rest_wakeup" not in service._active_task_kinds()
    asyncio.run(service.record_observed(source="host",task_name="replyer",request_type="replyer",model_name="",
                                       prompt_tokens=1,completion_tokens=2,total_tokens=3))
    assert store.record_llm_usage.await_args.kwargs["created_at"]==now.timestamp()
