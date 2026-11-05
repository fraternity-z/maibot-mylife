"""创作只回看已执行的真实活动，不把旧场景和计划变成经历。"""
import asyncio
from datetime import datetime, timedelta, timezone
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


NOW=datetime(2026,11,5,12,tzinfo=timezone(timedelta(hours=8)))


def service(plugin_module,nodes,scenes):
    module=importlib.import_module("mai_integration_tests.creation.inspiration_service")
    config=SimpleNamespace(creation=SimpleNamespace(inspiration_lookback_days=3))
    store=SimpleNamespace(list_diaries=AsyncMock(return_value=[]),
        get_framework=AsyncMock(side_effect=[nodes,[]]),get_scenes_by_framework_ids=AsyncMock(return_value=scenes),
        add_creation_inspiration=AsyncMock(return_value=True),scenes_for_day=AsyncMock())
    instance=module.InspirationService(Mock(),store,config,Mock(),Mock())
    instance._collect_external=AsyncMock(return_value=0)
    return instance


def test_life_inspiration_only_uses_executed_summary_without_private_plan_details(plugin_module):
    instance=service(plugin_module,[
        {"id":"task:done","summary":"待执行：私人书名和具体正文"},
        {"id":"task:pending","summary":"待执行：阅读小说"},
        {"id":"old-random-scene","summary":"在学校上课"},
    ],{
        "task:done":{"scene":"已阅读小说片段4000字并保存私人笔记"},
        "task:pending":{},"old-random-scene":{"scene":"吃完早餐去学校"},
    })
    assert asyncio.run(instance.collect(NOW))==1
    instance.store.get_scenes_by_framework_ids.assert_awaited_once_with(["task:done","task:pending"])
    item=instance.store.add_creation_inspiration.await_args.args[0]
    assert item["prompt_digest"]=="已阅读小说片段4000字并保存私人笔记"
    assert item["source_kind"]=="activity" and item["privacy_ceiling"]=="public"
    instance.store.scenes_for_day.assert_not_awaited()


@pytest.mark.parametrize("nodes,scenes",[
    ([],{}),
    ([{"id":"old","summary":"上课"}],{"old":{"scene":"做了晚饭"}}),
    ([{"id":"task:pending","summary":"待执行：阅读小说"}],{}),
    ([{"id":"task:empty","summary":"待执行：检索新闻"}],{"task:empty":{"scene":"  "}}),
])
def test_legacy_or_unexecuted_scenes_never_create_life_inspiration(plugin_module,nodes,scenes):
    instance=service(plugin_module,nodes,scenes)
    assert asyncio.run(instance.collect(NOW))==0
    instance.store.add_creation_inspiration.assert_not_awaited()
    instance.store.scenes_for_day.assert_not_awaited()


def test_diary_privacy_and_external_collection_are_preserved(plugin_module):
    instance=service(plugin_module,[],{})
    instance.store.list_diaries.return_value=[{
        "day":"2026-11-04","created_at":NOW.timestamp()-86400,
        "title":"私人日记","mood_summary":"平静","content":"实际执行记录",
    }]
    instance._collect_external.return_value=2
    assert asyncio.run(instance.collect(NOW))==3
    item=instance.store.add_creation_inspiration.await_args.args[0]
    assert item["source_kind"]=="diary" and item["privacy_ceiling"]=="private"
    instance._collect_external.assert_awaited_once_with(NOW)
