"""Planner 定时自主判断的真实存储集成测试。"""
import asyncio
from datetime import datetime, timedelta, timezone
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


NOW=datetime(2026,11,5,12,tzinfo=timezone(timedelta(hours=8)))


@pytest.fixture
def periodic(plugin_module,tmp_path):
    storage=importlib.import_module("mai_integration_tests.core.storage")
    config=plugin_module.MaiLifeSettings.model_validate({
        "proactive":{"patrol_interval_minutes":10},
        "users":{"profiles":[{"user_id":"123","daily_proactive_unlimited":True}]},
    })
    store=storage.LifeStore(str(tmp_path),clock=lambda:NOW.timestamp())

    async def initialize():
        await store.initialize()
        await store.sync_users(config.users.profiles)
        await store.set_user_stream("123","private-123")
        await store.save_state({"current_activity":"在阳台看书","current_location":"家里",
                                "last_updated_at":NOW.timestamp()})

    asyncio.run(initialize())
    trigger=AsyncMock(return_value={"success":True,"task_id":"task"})
    ctx=SimpleNamespace(maisaka=SimpleNamespace(proactive=SimpleNamespace(trigger=trigger)))
    engine=plugin_module.ProactiveEngine(ctx,store,config,None,Mock())
    yield SimpleNamespace(engine=engine,config=config,store=store,trigger=trigger)
    asyncio.run(store.close())


def test_empty_opportunity_pool_still_asks_planner_with_real_life_context(periodic):
    periodic.config.context.enabled=False
    assert asyncio.run(periodic.engine.patrol(NOW)) is True
    reason=json.loads(periodic.trigger.await_args.kwargs["reason"])
    assert reason["decision_kind"]=="periodic"
    assert reason["current_life"]=={"activity":"在阳台看书","location":"家里"}
    assert reason["relationship_role"]=="friend"
    assert "不是已经发生的生活事件" in reason["instruction"] and "不要虚构经历" in reason["instruction"]
    assert "可以沉默" in reason["instruction"]
    row=periodic.store.conn.execute("SELECT target_user_id,consumed_by FROM proactive_opportunities").fetchone()
    assert tuple(row)==("123","123")
    assert asyncio.run(periodic.store.get_user("123"))["proactive_count"]==0


@pytest.mark.parametrize("weight,target,kind",[(0.9,"123","opportunity"),(0,"123","periodic"),(0.9,"456","periodic")])
def test_eligible_life_reference_is_preferred_without_blocking_periodic_judgment(periodic,weight,target,kind):
    asyncio.run(periodic.store.add_opportunity({"id":"scene","framework_id":"life:today",
        "topic":"生活参考","motive":"分享生活","weight":weight,"target_user_id":target,
        "privacy":"normal","expires_at":NOW.timestamp()+3600}))
    assert asyncio.run(periodic.engine.patrol(NOW)) is True
    reason=json.loads(periodic.trigger.await_args.kwargs["reason"])
    assert reason["decision_kind"]==kind
    assert (reason["topic"]=="生活参考") is (kind=="opportunity")
    if kind=="periodic":
        assert periodic.store.conn.execute("SELECT consumed_at FROM proactive_opportunities WHERE id='scene'").fetchone()[0]==0


@pytest.mark.parametrize("boundary",["user_disabled","proactive_disabled","no_stream","quiet","daily_limit",
                                       "recent_message","recent_send","work_hours","plugin_disabled"])
def test_periodic_judgment_does_not_bypass_user_and_timing_limits(periodic,boundary):
    update={"user_disabled":"enabled=0","proactive_disabled":"proactive_enabled=0",
            "no_stream":"stream_id=''","quiet":"quiet_start='11:00',quiet_end='13:00'",
            "daily_limit":"daily_proactive_unlimited=0,daily_proactive_max=0",
            "recent_message":f"last_user_message_at={NOW.timestamp()-60}",
            "recent_send":f"last_proactive_at={NOW.timestamp()-60}"}
    if boundary in update:
        periodic.store.conn.execute("UPDATE users SET "+update[boundary]+" WHERE user_id='123'")
        periodic.store.conn.commit()
    elif boundary=="work_hours":
        periodic.config.rest_gate.work_start="13:00"; periodic.config.rest_gate.work_end="14:00"
    elif boundary=="plugin_disabled":periodic.config.proactive.enabled=False
    assert asyncio.run(periodic.engine.patrol(NOW)) is False
    periodic.trigger.assert_not_awaited()
    assert periodic.store.conn.execute("SELECT COUNT(*) FROM proactive_events").fetchone()[0]==0


@pytest.mark.parametrize("host_success",[True,False])
def test_silence_and_failed_triggers_wait_until_the_next_interval_even_after_restart(periodic,host_success):
    periodic.trigger.return_value={"success":host_success,"task_id":"task"}
    assert asyncio.run(periodic.engine.patrol(NOW)) is host_success
    asyncio.run(periodic.store.close())
    asyncio.run(periodic.store.initialize())
    for seconds in (180,599):
        assert asyncio.run(periodic.engine.patrol(NOW+timedelta(seconds=seconds))) is False
    assert periodic.trigger.await_count==1
    periodic.trigger.return_value={"success":True,"task_id":"task-2"}
    assert asyncio.run(periodic.engine.patrol(NOW+timedelta(minutes=10))) is True
    assert periodic.trigger.await_count==2
    assert periodic.store.conn.execute("SELECT COUNT(*) FROM proactive_events").fetchone()[0]==2
    assert asyncio.run(periodic.store.get_user("123"))["proactive_count"]==0


def test_actual_send_and_planner_judgment_use_the_same_interval(periodic):
    assert asyncio.run(periodic.engine.patrol(NOW)) is True
    event=periodic.store.conn.execute("SELECT id FROM proactive_events").fetchone()[0]
    assert asyncio.run(periodic.store.mark_pending_sent("private-123",NOW.timestamp()+60,day=str(NOW.date()),event_id=event)) is True
    assert asyncio.run(periodic.engine.patrol(NOW+timedelta(minutes=10))) is False
    assert asyncio.run(periodic.engine.patrol(NOW+timedelta(minutes=11))) is True
    assert periodic.trigger.await_count==2
    assert asyncio.run(periodic.store.get_user("123"))["proactive_count"]==1


def test_multiple_users_take_turns_instead_of_the_same_user_repeatedly(periodic):
    profile=type(periodic.config.users.profiles[0])(user_id="456",daily_proactive_unlimited=True)
    periodic.config.users.profiles.append(profile)
    asyncio.run(periodic.store.sync_users(periodic.config.users.profiles))
    asyncio.run(periodic.store.set_user_stream("456","private-456"))
    assert asyncio.run(periodic.engine.patrol(NOW)) is True
    assert asyncio.run(periodic.engine.patrol(NOW+timedelta(minutes=1))) is True
    streams=[call.kwargs["stream_id"] for call in periodic.trigger.await_args_list]
    assert set(streams)=={"private-123","private-456"}
    assert asyncio.run(periodic.engine.patrol(NOW+timedelta(minutes=2))) is False


def test_lost_pending_registration_race_releases_the_consumed_reference(periodic):
    periodic.store.add_proactive_pending=AsyncMock(return_value=False)
    assert asyncio.run(periodic.engine.patrol(NOW)) is False
    periodic.trigger.assert_not_awaited()
    assert periodic.store.conn.execute("SELECT consumed_at FROM proactive_opportunities").fetchone()[0]==0


def test_hot_interval_change_does_not_reuse_an_old_consumed_periodic_candidate(periodic):
    # 同一编号可能来自旧间隔下的历史桶，配置变化不能让新判断卡在已消费契机上。
    bucket=int(NOW.timestamp()//600)
    asyncio.run(periodic.store.add_opportunity({"id":f"planner:123:660:{bucket}","framework_id":"planner:old",
        "topic":"旧判断","motive":"旧周期","weight":0,"target_user_id":"123",
        "expires_at":NOW.timestamp()+3600}))
    asyncio.run(periodic.store.consume_opportunity(f"planner:123:660:{bucket}","123",NOW.timestamp()-86400))
    assert asyncio.run(periodic.engine.patrol(NOW)) is True
    reason=json.loads(periodic.trigger.await_args.kwargs["reason"])
    assert reason["decision_kind"]=="periodic" and reason["topic"]!="旧判断"
