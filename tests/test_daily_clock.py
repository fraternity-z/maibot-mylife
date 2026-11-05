"""每日生成循环必须在联网校时后重新判断本地日期和生成时点。"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest


@pytest.fixture
def plugin(plugin_module):
    instance=plugin_module.MaiLifePlugin()
    instance.config=plugin_module.MaiLifeSettings()
    instance.config.schedule.generate_hour=8
    instance.ctx=SimpleNamespace(logger=Mock())
    instance._env=Mock(refresh_weather=AsyncMock(return_value={}))
    instance._schedule=SimpleNamespace(ensure_day=AsyncMock())
    instance._memory=SimpleNamespace(schedule_context=AsyncMock(return_value={}))
    return instance


def at(day,hour,minute=0):
    return datetime(2026,11,day,hour,minute,tzinfo=timezone(timedelta(hours=8)))


def run_ticks(plugin,initial,ticks):
    plugin._env.now.return_value=initial
    moments=iter(ticks)

    async def tick(delay):
        assert delay==60
        try:plugin._env.now.return_value=next(moments)
        except StopIteration:raise asyncio.CancelledError

    with patch("asyncio.sleep",side_effect=tick) as sleep:
        with pytest.raises(asyncio.CancelledError):asyncio.run(plugin._daily_generation_loop())
    return sleep


def generated(plugin):
    return [call.args[0] for call in plugin._schedule.ensure_day.await_args_list]


def test_start_after_generate_hour_skips_today_and_runs_next_day(plugin):
    run_ticks(plugin,at(5,10),[at(5,11),at(6,7,59),at(6,8),at(6,8,1),at(6,18)])
    assert generated(plugin)==[at(6,8)]
    assert plugin._schedule.ensure_day.await_args.kwargs["force"] is True


def test_initial_wrong_clock_then_network_correction_runs_current_day(plugin):
    # 启动误以为已到明天；首次成功校准回今天后，不沿用原来的全天 sleep。
    run_ticks(plugin,at(6,7),[at(5,7,59),at(5,8),at(5,8,1)])
    assert generated(plugin)==[at(5,8)]


def test_forward_clock_correction_skips_missed_dates(plugin):
    run_ticks(plugin,at(3,7),[at(5,8),at(5,9),at(6,8)])
    assert generated(plugin)==[at(5,8),at(6,8)]


def test_clock_rewind_does_not_generate_same_date_twice(plugin):
    run_ticks(plugin,at(5,7),[at(5,8),at(6,8),at(5,9),at(6,9)])
    assert generated(plugin)==[at(5,8),at(6,8)]


def test_rechecks_time_after_weather_request(plugin):
    async def refresh():
        plugin._env.now.return_value=at(5,7)
        return {}
    plugin._env.refresh_weather.side_effect=refresh
    run_ticks(plugin,at(5,7),[at(6,8)])
    plugin._schedule.ensure_day.assert_not_awaited()


def test_generation_failure_waits_before_retry_and_cancellation_propagates(plugin):
    plugin._schedule.ensure_day.side_effect=[RuntimeError("temporary failure"),[]]
    sleep=run_ticks(plugin,at(5,7),[at(5,8),at(5,8,1),at(5,9)])
    assert generated(plugin)==[at(5,8),at(5,8,1)]
    assert sleep.call_count==4
    plugin.ctx.logger.error.assert_called_once()


def test_cancellation_during_generation_propagates(plugin):
    plugin._schedule.ensure_day.side_effect=asyncio.CancelledError
    sleep=run_ticks(plugin,at(5,7),[at(5,8)])
    assert sleep.call_count==1
    plugin.ctx.logger.error.assert_not_called()
