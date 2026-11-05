"""实际活动执行与校准时间的集成回归，外部联网和模型均隔离。"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest


PRIVATE_TITLE="仅主人可见的小说"
PRIVATE_TEXT="这是私人小说中的原文，不能出现在公共活动记录或错误日志。"


def at(day=5,hour=10,minute=0):
    return datetime(2026,11,day,hour,minute,tzinfo=timezone(timedelta(hours=8)))


@pytest.fixture
def plugin(plugin_module,tmp_path):
    instance=plugin_module.MaiLifePlugin()
    instance.config=plugin_module.MaiLifeSettings()
    instance.config.network.enabled=False
    instance.config.creation.enabled=False
    instance.config.reading.chunk_chars=500
    instance.ctx=SimpleNamespace(logger=Mock())
    instance._env=Mock(now=Mock(return_value=at()),refresh_time=AsyncMock(),refresh_weather=AsyncMock(return_value={}))
    instance._store=plugin_module.LifeStore(str(tmp_path/"data"),clock=lambda:instance._env.now().timestamp())
    asyncio.run(instance._store.initialize())
    instance._llm=Mock(task_available=Mock(return_value=False),generate=AsyncMock())
    instance._schedule=plugin_module.ScheduleService(instance._store,instance.config,instance._llm,str(tmp_path),instance.ctx.logger)
    instance._reading=plugin_module.ReadingService(instance._store,instance.config,instance._llm,str(tmp_path),instance.ctx.logger)
    instance._information=Mock(tick=AsyncMock(return_value={"news":0,"search":0,"associated":0}))
    instance._memory=SimpleNamespace(ensure_daily=AsyncMock())
    for name in ("_rest","_proactive","_continuity","_group_observer","_relay","_bookshelf","_creation","_admin"):
        setattr(instance,name,Mock())
    instance._settle_relationships=AsyncMock()
    novels=tmp_path/"novels"; novels.mkdir()
    (novels/f"{PRIVATE_TITLE}.txt").write_text(PRIVATE_TEXT*100,encoding="utf-8")
    yield instance
    asyncio.run(instance._store.close())


async def context(plugin):return await plugin._schedule.context(plugin._env.now())


def assert_private_text_absent(plugin,*values):
    public=str(values)
    logs=str(plugin.ctx.logger.mock_calls)
    for secret in (PRIVATE_TITLE,PRIVATE_TEXT):
        assert secret not in public
        assert secret not in logs


def test_reading_executes_with_network_and_creation_disabled(plugin):
    async def scenario():
        await plugin._schedule.ensure_day(at())
        before=await context(plugin)
        assert "待执行" in before["current"]["summary"] and before["scene"]=={}
        await plugin._activity_tick()
        current=await context(plugin); state=await plugin._store.get_state()
        assert current["scene"]["scene"]=="10:00 已阅读小说片段500字并保存私人笔记"
        assert state["current_activity"]=="已阅读小说片段500字并保存私人笔记"
        assert await plugin._store.get_scene(current["next"]["id"])=={}
        rows=await plugin._store.recent_local_reading_notes()
        assert len(rows)==1 and rows[0]["excerpt"]==(PRIVATE_TEXT*100)[:500]
        assert await plugin._store.list_bookshelf_documents(allow_private=False)==[]
        assert await plugin._store.active_opportunities(at().timestamp())==[]
        assert_private_text_absent(plugin,current,state)
        plugin._information.tick.assert_not_awaited()
        plugin._llm.generate.assert_not_awaited()
    asyncio.run(scenario())


def test_interval_and_restarted_reader_continue_without_repeating(plugin,plugin_module,tmp_path):
    async def scenario():
        await plugin._activity_tick()
        plugin._env.now.return_value=at(minute=30)
        await plugin._activity_tick()
        assert len(await plugin._store.recent_local_reading_notes())==1
        assert "等待下次阅读间隔" in (await plugin._store.get_state())["current_activity"]
        plugin._reading=plugin_module.ReadingService(plugin._store,plugin.config,plugin._llm,str(tmp_path),plugin.ctx.logger)
        plugin._activity_status={}
        await plugin._activity_tick()
        assert len(await plugin._store.recent_local_reading_notes())==1
        plugin._env.now.return_value=at(hour=11)
        await plugin._activity_tick()
        rows=await plugin._store.recent_local_reading_notes()
        assert [(row["start_offset"],row["end_offset"]) for row in rows]==[(500,1000),(0,500)]
        assert rows[0]["excerpt"]==(PRIVATE_TEXT*100)[500:1000]
        current=await context(plugin)
        assert current["scene"]["scene"].splitlines()==["10:00 已阅读小说片段500字并保存私人笔记",
                                                        "11:00 已阅读小说片段500字并保存私人笔记"]
        assert await plugin._store.get_scene(current["next"]["id"])=={}
    asyncio.run(scenario())


@pytest.mark.parametrize("reason",["disabled","missing","quota"])
def test_unexecuted_reading_is_status_without_completed_scene(plugin,tmp_path,reason):
    async def scenario():
        if reason=="disabled":plugin.config.reading.enabled=False
        elif reason=="missing":(tmp_path/"novels"/f"{PRIVATE_TITLE}.txt").unlink()
        else:plugin.config.reading.daily_max=0
        await plugin._activity_tick()
        current=await context(plugin)
        assert current["scene"]=={}
        assert "待执行" in current["current"]["summary"]
        state=await plugin._store.get_state()
        assert "已阅读" not in state["current_activity"]
        assert await plugin._store.recent_local_reading_notes()==[]
        assert_private_text_absent(plugin,current,state)
    asyncio.run(scenario())


@pytest.mark.parametrize("moment",[at(hour=0),at(hour=7,minute=59),at(hour=23,minute=30),at(hour=23,minute=59),at(day=6,hour=0)])
def test_sleep_and_midnight_do_not_execute_tasks(plugin,moment):
    async def scenario():
        plugin._env.now.return_value=moment; plugin.config.network.enabled=True
        with patch.object(plugin._reading,"tick",wraps=plugin._reading.tick) as reader:
            await plugin._activity_tick()
            reader.assert_not_called()
        current=await context(plugin)
        assert current["current"]["kind"]=="sleep" and current["scene"]=={}
        assert await plugin._store.recent_local_reading_notes()==[]
        plugin._information.tick.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("news,search,associated,expected",[(0,0,0,""),(0,0,3,""),
    (2,0,2,"已检索并保存 2 条新闻资料"),(0,1,0,"已检索主题资料并保存学习笔记"),
    (2,1,2,"已检索并保存 2 条新闻资料")])
def test_only_successful_news_or_search_counts_create_activity(plugin,news,search,associated,expected):
    async def scenario():
        plugin.config.network.enabled=True; plugin._env.now.return_value=at(hour=9)
        plugin._information.tick.return_value={"news":news,"search":search,"associated":associated,
                                               "summary":PRIVATE_TEXT,"title":PRIVATE_TITLE}
        await plugin._activity_tick()
        current=await context(plugin); state=await plugin._store.get_state()
        if expected:
            assert expected in current["scene"]["scene"]
            assert current["scene"]["scene"]=="09:00 "+state["current_activity"]
        else:
            assert current["scene"]=={}
            assert "本轮无新增检索资料" in state["current_activity"]
        plugin._information.tick.assert_awaited_once()
        assert await plugin._store.recent_local_reading_notes()==[]
        assert await plugin._store.get_scene(current["next"]["id"])=={}
        assert_private_text_absent(plugin,current,state)
    asyncio.run(scenario())


def test_network_disabled_does_not_claim_news_were_retrieved(plugin):
    async def scenario():
        plugin._env.now.return_value=at(hour=9)
        await plugin._activity_tick()
        assert (await context(plugin))["scene"]=={}
        assert (await plugin._store.get_state())["current_activity"]=="联网已关闭，资料检索未执行"
        plugin._information.tick.assert_not_awaited()
    asyncio.run(scenario())


def test_network_error_is_logged_without_response_text_or_completed_scene(plugin):
    async def scenario():
        plugin.config.network.enabled=True; plugin._env.now.return_value=at(hour=9)
        plugin._information.tick.side_effect=RuntimeError(PRIVATE_TEXT)
        with patch("asyncio.sleep",new=AsyncMock(side_effect=[None,asyncio.CancelledError])):
            with pytest.raises(asyncio.CancelledError):await plugin._information_loop()
        assert (await context(plugin))["scene"]=={}
        plugin.ctx.logger.error.assert_called_once()
        assert "RuntimeError" in plugin.ctx.logger.error.call_args.args[0]
        assert_private_text_absent(plugin,await plugin._store.get_state())
    asyncio.run(scenario())


def test_reading_save_failure_keeps_progress_and_public_state_clean(plugin):
    async def scenario():
        with patch.object(plugin._store,"save_reading_chunk",new=AsyncMock(side_effect=RuntimeError(PRIVATE_TEXT))):
            await plugin._activity_tick()
        assert (await context(plugin))["scene"]=={}
        assert await plugin._store.recent_local_reading_notes()==[]
        state=await plugin._store.get_state()
        assert "小说读取失败" in state["current_activity"]
        plugin.ctx.logger.warning.assert_called_once()
        assert "RuntimeError" in plugin.ctx.logger.warning.call_args.args[0]
        assert_private_text_absent(plugin,state)
    asyncio.run(scenario())


def test_invalid_utf8_source_is_reported_without_private_filename(plugin,tmp_path):
    async def scenario():
        (tmp_path/"novels"/f"{PRIVATE_TITLE}.txt").write_bytes(b"\xff\xfeinvalid")
        await plugin._activity_tick()
        assert (await context(plugin))["scene"]=={}
        assert await plugin._store.recent_local_reading_notes()==[]
        plugin.ctx.logger.info.assert_called_once()
        assert "UnicodeDecodeError" in plugin.ctx.logger.info.call_args.args[0]
        assert_private_text_absent(plugin,await plugin._store.get_state())
    asyncio.run(scenario())


def test_maintenance_uses_corrected_date_and_does_not_execute_plans(plugin):
    async def scenario():
        stale=at(hour=23,minute=59); corrected=at(day=6,hour=10)
        plugin._env.now.return_value=stale
        async def calibrate():plugin._env.now.return_value=corrected
        plugin._env.refresh_time.side_effect=calibrate
        with patch.object(plugin._schedule,"ensure_day",wraps=plugin._schedule.ensure_day) as ensure:
            await plugin._maintenance_tick(allow_weather_network=False)
            ensure.assert_awaited_once_with(corrected)
        assert await plugin._store.get_framework(stale.date().isoformat())==[]
        current=await context(plugin); state=await plugin._store.get_state()
        assert current["current"]["day"]==corrected.date().isoformat()
        assert "待执行" in state["current_activity"] and current["scene"]=={}
        assert state["last_updated_at"]==corrected.timestamp()
        plugin._memory.ensure_daily.assert_awaited_once_with(corrected)
        assert await plugin._store.recent_local_reading_notes()==[]
        plugin._information.tick.assert_not_awaited()
    asyncio.run(scenario())


def test_concurrent_activity_ticks_do_not_repeat_the_same_reading(plugin):
    async def scenario():
        plugin._llm.task_available.return_value=True
        async def annotate(*args,**kwargs):
            await asyncio.sleep(0)
            return "仅基于实际片段的私人阅读笔记"
        plugin._llm.generate.side_effect=annotate
        await asyncio.gather(plugin._activity_tick(),plugin._activity_tick(),plugin._activity_tick())
        rows=await plugin._store.recent_local_reading_notes()
        assert len(rows)==1 and rows[0]["end_offset"]==500
        plugin._llm.generate.assert_awaited_once()
        assert (await context(plugin))["scene"]["scene"]=="10:00 已阅读小说片段500字并保存私人笔记"
        assert_private_text_absent(plugin,await plugin._store.get_state())
    asyncio.run(scenario())
