"""日常活动继续参与背景和创作，旧体征字段不再参与行为。"""
import asyncio
from datetime import datetime, timedelta, timezone
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


NOW=datetime(2026,11,5,12,tzinfo=timezone(timedelta(hours=8)))


@pytest.fixture
def modules(plugin_module):
    names=("messaging.prompt_builder","life.proactive","creation.creation_service",
           "creation.inspiration_service","life.memory_service","information.information_service")
    return SimpleNamespace(**{name.rsplit(".",1)[-1]:importlib.import_module(f"mai_integration_tests.{name}") for name in names})


@pytest.mark.parametrize("kind,activity",[("meal","在厨房准备午饭"),("sleep","在卧室睡觉"),("nap","午后在沙发小憩")])
def test_prompts_keep_daily_activities_and_ignore_legacy_vitals(modules,kind,activity):
    builder=modules.prompt_builder.PromptBuilder()
    state={"current_location":"家里","current_activity":activity,"energy":0,"hunger":100,
           "mood_valence":-1,"mood_arousal":1,"sleep_phase":"woken","health_note":"旧健康备注"}
    context={"current":{"kind":kind,"summary":activity,"location":"家里"}}
    for prompt in (builder.planner(state,{},context,{}),builder.replyer(state,{},context,{})):
        assert activity in prompt and "家里" in prompt
        assert all(term not in prompt for term in ("精力","饥饿","精神活跃度","睡眠阶段","旧健康备注","刚被叫醒","梦境余韵"))
        assert "不是用户刚说的" in prompt
        assert "未回应摘要" not in prompt


@pytest.mark.parametrize("kind,force,expected",[("leisure",False,"no_inspiration"),("work",False,"schedule_busy"),("work",True,"no_inspiration")])
def test_creation_uses_schedule_without_a_vitals_store(modules,kind,force,expected):
    config=SimpleNamespace(creation=SimpleNamespace(enabled=True,plaintext_storage_acknowledged=True,
                                                    daily_max=2,allowed_schedule_types=["leisure"]))
    store=SimpleNamespace(recover_creation_claims=AsyncMock(),archived_work_count=AsyncMock(return_value=0),
                          pending_creation_inspirations=AsyncMock(return_value=[]))
    service=modules.creation_service.CreationService(Mock(),store,config,Mock(),Mock())
    service.inspirations.collect=AsyncMock()
    result=asyncio.run(service.tick(NOW,"日常人格",{"current":{"kind":kind}},force=force))
    assert result=={"status":expected}
    if expected=="schedule_busy":service.inspirations.collect.assert_not_awaited()
    else:service.inspirations.collect.assert_awaited_once_with(NOW)


@pytest.mark.parametrize("host_success",[True,False])
def test_proactive_needs_no_vitals_and_preserves_failed_trigger_cleanup(modules,host_success):
    config=SimpleNamespace(rest_gate=SimpleNamespace(enabled=False),proactive=SimpleNamespace(
        enabled=True,patrol_interval_minutes=30,max_retries_per_opportunity=2,recent_user_silence_minutes=10,
        score_threshold=0.5,pending_expire_seconds=300))
    opportunity={"id":"o","weight":0.8,"topic":"午饭做了新菜","motive":"分享日常","privacy":"normal"}
    user={"user_id":"u","stream_id":"s","quiet_start":"00:00","quiet_end":"00:00",
          "daily_proactive_max":2,"temperature":30}
    store=SimpleNamespace(expire_pending=AsyncMock(),active_opportunities=AsyncMock(return_value=[opportunity]),
        last_proactive_checks=AsyncMock(return_value={}),
        list_users=AsyncMock(return_value=[user]),active_hours_batch=AsyncMock(return_value={}),
        pending_proactive_users=AsyncMock(return_value=set()),
        consume_opportunity=AsyncMock(return_value=True),add_proactive_pending=AsyncMock(),
        set_proactive_task_id=AsyncMock(return_value=True),release_opportunity=AsyncMock(),
        set_proactive_event_status=AsyncMock(),record_proactive_skip=AsyncMock())
    trigger=AsyncMock(return_value={"success":host_success,"task_id":"task"})
    context=SimpleNamespace(maisaka=SimpleNamespace(proactive=SimpleNamespace(trigger=trigger)))
    service=modules.proactive.ProactiveEngine(context,store,config,None,Mock())
    assert asyncio.run(service.patrol(NOW)) is host_success
    reason=json.loads(trigger.await_args.kwargs["reason"])
    assert reason["topic"]==opportunity["topic"]
    assert reason["score"]==0.86
    if host_success:store.release_opportunity.assert_not_awaited()
    else:
        store.release_opportunity.assert_awaited_once_with("o")
        assert store.set_proactive_event_status.await_args.args[1]=="failed"


def test_inspiration_collects_diary_and_scenes_without_dream_storage(modules):
    config=SimpleNamespace(creation=SimpleNamespace(inspiration_lookback_days=3,external_reading_enabled=False,
                                                    external_reading_api_name=""))
    diary={"day":"2026-11-04","created_at":NOW.timestamp()-86400,"title":"新闻阅读",
           "mood_summary":"平稳","content":"整理了新闻"}
    store=SimpleNamespace(list_diaries=AsyncMock(return_value=[diary]),
        get_framework=AsyncMock(side_effect=[[{"id":"task:news","kind":"leisure","summary":"待执行：检索新闻"}],[]]),
        get_scenes_by_framework_ids=AsyncMock(return_value={"task:news":{"scene":"已检索新闻并保存摘要"}}),
        add_creation_inspiration=AsyncMock(return_value=True))
    service=modules.inspiration_service.InspirationService(Mock(),store,config,Mock(),Mock())
    assert asyncio.run(service.collect(NOW))==2
    items=[call.args[0] for call in store.add_creation_inspiration.await_args_list]
    assert [(item["source_kind"],item["privacy_ceiling"]) for item in items]==[("diary","private"),("activity","public")]
    assert "已检索新闻并保存摘要" in items[1]["prompt_digest"]
    assert "待执行" not in items[1]["prompt_digest"]


def test_diary_uses_executed_activities_and_anonymous_counts_without_dream_storage(modules):
    config=SimpleNamespace(memory=SimpleNamespace(diary_max_chars=1000))
    store=SimpleNamespace(get_framework=AsyncMock(return_value=[
        {"id":"task:news","kind":"leisure","summary":"待执行：检索新闻","location":"在线"}]),
        get_scenes_by_framework_ids=AsyncMock(return_value={"task:news":{"scene":"检索并整理了2条新闻摘要"}}),
        interaction_counts=AsyncMock(return_value=[{"user_id":"private-user","count":4}]),
        save_diary=AsyncMock(),list_users=AsyncMock(return_value=[]))
    llm=SimpleNamespace(task_available=Mock(return_value=True),generate_json=AsyncMock(return_value={
        "title":"新闻阅读","content":"整理了新闻摘要","mood_summary":"有些收获"}))
    service=modules.memory_service.MemoryService(store,config,llm,Mock())
    asyncio.run(service._generate_diary(NOW,NOW.date()-timedelta(days=1)))
    prompt=llm.generate_json.await_args.args[0]
    source=json.loads(prompt.split("\n",1)[1])
    assert "dreams" not in source and "private-user" not in prompt
    assert source["interaction_counts"]=={"total":4,"active_people":1}
    assert source["life"][0]["summary"]=="检索并整理了2条新闻摘要"
    assert "待执行" not in source["life"][0]["summary"]
    assert "不得虚构" in prompt
    assert store.save_diary.await_args.args[3]=="有些收获"


@pytest.mark.parametrize("nodes,scenes",[
    ([],{}),
    ([{"id":"old-scene","kind":"meal"}],{"old-scene":{"scene":"在厨房做饭"}}),
    ([{"id":"task:reading","kind":"study"}],{}),
    ([{"id":"task:reading","kind":"study"}],{"task:reading":{"scene":" "}}),
])
def test_diary_skips_unexecuted_and_legacy_activities(modules,nodes,scenes):
    store=SimpleNamespace(get_framework=AsyncMock(return_value=nodes),
        get_scenes_by_framework_ids=AsyncMock(return_value=scenes),save_diary=AsyncMock(),
        interaction_counts=AsyncMock())
    llm=SimpleNamespace(generate_json=AsyncMock())
    service=modules.memory_service.MemoryService(store,Mock(),llm,Mock())
    asyncio.run(service._generate_diary(NOW,NOW.date()-timedelta(days=1)))
    store.save_diary.assert_not_awaited()
    store.interaction_counts.assert_not_awaited()
    llm.generate_json.assert_not_awaited()


def test_diary_backfill_never_fabricates_offline_days_and_is_idempotent(modules):
    recorded_day=(NOW.date()-timedelta(days=2)).isoformat(); saved={}
    nodes=[{"id":"task:reading","kind":"study","location":"在线","summary":"待执行：阅读小说"}]
    store=SimpleNamespace(get_diary=AsyncMock(side_effect=lambda day:saved.get(day,{})),
        get_framework=AsyncMock(side_effect=lambda day:nodes if day==recorded_day else []),
        get_scenes_by_framework_ids=AsyncMock(return_value={"task:reading":{"scene":"已阅读小说片段并保存笔记"}}),
        interaction_counts=AsyncMock(return_value=[]),save_diary=AsyncMock(side_effect=lambda day,*args:saved.update({day:args})),
        list_users=AsyncMock(return_value=[]))
    llm=SimpleNamespace(task_available=Mock(return_value=False),generate_json=AsyncMock())
    service=modules.memory_service.MemoryService(store,SimpleNamespace(memory=SimpleNamespace(diary_max_chars=1000)),llm,Mock())
    asyncio.run(service._backfill_diaries(NOW))
    asyncio.run(service._backfill_diaries(NOW))
    assert list(saved)==[recorded_day]
    store.save_diary.assert_awaited_once()
    assert saved[recorded_day][0]==f"{recorded_day} 活动记录"
    assert "已阅读小说片段并保存笔记" in saved[recorded_day][1]
    llm.generate_json.assert_not_awaited()


@pytest.mark.parametrize("role",["owner","friend"])
def test_prompts_show_execution_and_sources_and_keep_reading_private(modules,role):
    builder=modules.prompt_builder.PromptBuilder()
    context={"current":{"id":"task:news","kind":"leisure","summary":"待执行：检索新闻"},
             "scene":{"scene":"检索了2条新闻摘要"}}
    information={"news":[{"title":"测试新闻","summary":"新闻摘要","url":"https://example.org/news","read_scope":"snippet"}],
                 "reading":{"items":[{"title":"私人小说","source":"novels/example.txt","range":"1-4/20",
                                          "excerpt":"私人阅读摘录","annotation":"私人阅读笔记"}]}}
    for method in (builder.planner,builder.replyer):
        prompt=method({"current_activity":"旧的虚构场景"},{},context,{"role":role},information=information,max_chars=8000)
        assert "检索了2条新闻摘要" in prompt and "旧的虚构场景" not in prompt
        assert "计划和旧场景不代表完成" in prompt and "不代表模型训练" in prompt
        assert "仅检索摘要" in prompt and "https://example.org/news" in prompt
        assert ("私人阅读摘录" in prompt)==(role=="owner")
        assert ("私人阅读笔记" in prompt)==(role=="owner")


def test_prompts_do_not_recognize_old_generated_scenes_as_execution(modules):
    builder=modules.prompt_builder.PromptBuilder()
    context={"current":{"id":"legacy","summary":"旧计划"},"scene":{"scene":"虚构的旧场景"}}
    for method in (builder.planner,builder.replyer):
        prompt=method({"current_activity":"虚构的旧场景"},{},context,{})
        assert "虚构的旧场景" not in prompt
        assert "当前时段尚无实际执行记录" in prompt


def test_reading_notes_keep_a_context_budget_when_news_is_long(modules):
    information={"news":[{"title":"近期新闻","summary":"新闻内容"*1000}],
                 "reading":{"items":[{"title":"小说","source":"novels/story.txt","annotation":"已读片段的批注",
                                          "excerpt":"小说原文"*1000}]}}
    text=modules.prompt_builder.PromptBuilder._information_text(information,is_owner=True)
    assert "近期新闻" in text and "已读片段的批注" in text
    assert len(text)<1250


def test_news_article_knowledge_reaches_prompts_with_reading_notes(modules):
    information={"news":[{"title":"测试新闻","summary":"仅有标题摘要","url":"https://example.org/article",
                           "read_scope":"article","article_excerpt":"正文中的新增知识。"+"正文"*1000}],
                 "reading":{"items":[{"title":"小说","annotation":"已读小说片段批注"}]}}
    builder=modules.prompt_builder.PromptBuilder()
    for method in (builder.planner,builder.replyer):
        prompt=method({}, {}, {}, {"role":"owner"},information=information,max_chars=8000)
        assert "已读正文" in prompt and "正文中的新增知识" in prompt
        assert "https://example.org/article" in prompt and "已读小说片段批注" in prompt


@pytest.mark.parametrize("method",["associate","plan_query"])
def test_information_models_receive_daily_activity_without_legacy_metrics(modules,method):
    config=SimpleNamespace(network=SimpleNamespace(association_enabled=True,search_include_chat_topics=False))
    llm=SimpleNamespace(task_available=Mock(return_value=True),generate_json=AsyncMock(return_value={}))
    service=modules.information_service.InformationService(Mock(),Mock(),config,llm,Mock())
    service.search.sanitize_query=AsyncMock(side_effect=lambda value:value)
    state={"current_activity":"整理读书笔记","energy":0,"mood_valence":-1}
    schedule={"current":{"kind":"study","summary":"整理读书笔记"}}
    if method=="associate":asyncio.run(service._associate({"title":"读书"},"日常人格",state,schedule))
    else:asyncio.run(service._plan_query(NOW,"日常人格",state,schedule,[]))
    prompt=llm.generate_json.await_args.args[0]
    context=json.loads(prompt.split("\n",1)[1].rsplit("\n",1)[0])
    assert context["state"]=={"activity":"整理读书笔记"}
    assert context["schedule"]["current"]["kind"]=="study"
