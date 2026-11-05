"""真实检索的降级、去重和来源边界。"""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


NOW=datetime(2026,11,5,9,tzinfo=timezone(timedelta(hours=8)))
SCHEDULE={"current":{"kind":"leisure","summary":"待执行：检索新闻和主题资料"}}
HIT={"title":"科学新闻","url":"https://example.org/news","snippet":"搜索摘要","published_at":""}


@pytest.fixture
def services(plugin_module):
    return SimpleNamespace(
        news=importlib.import_module("mai_integration_tests.information.news_service"),
        information=importlib.import_module("mai_integration_tests.information.information_service"),
    )


def config():
    return SimpleNamespace(network=SimpleNamespace(
        enabled=True,news_enabled=True,news_daily_max=2,news_allowed_schedule_types=["leisure"],
        max_results=5,news_full_text_count=1,news_max_concurrency=1,news_max_article_chars=5000,
        news_retention_days=7,search_enabled=True,search_daily_max=2,search_allowed_schedule_types=["leisure"],
        search_note_retention_days=7,context_item_limit=3,association_threshold=0.4,
        proactive_share_enabled=False,
    ))


def news_service(services,*,results=None):
    store=SimpleNamespace(search_attempt_count=AsyncMock(return_value=0),upsert_news_item=AsyncMock(return_value=True))
    search=SimpleNamespace(available=Mock(return_value=True),sanitize_query=AsyncMock(side_effect=lambda value:value),
        search=AsyncMock(return_value={"results":[] if results is None else results,"service":"test","error_class":""}),
        fetch_article=AsyncMock(return_value=""))
    llm=SimpleNamespace(task_available=Mock(return_value=False),generate_json=AsyncMock())
    return services.news.NewsService(store,config(),search,Mock(),llm)


def test_news_without_model_still_searches_sanitized_dated_query(services):
    service=news_service(services,results=[dict(HIT)])
    assert asyncio.run(service.refresh_due(NOW,SCHEDULE,""))==1
    service.search.sanitize_query.assert_awaited_once_with("今日新闻 2026-11-05")
    service.search.search.assert_awaited_once_with("今日新闻 2026-11-05",operation="news",max_results=5)
    service.llm.generate_json.assert_not_awaited()
    saved=service.store.upsert_news_item.await_args.args[0]
    assert saved["summary"]=="搜索摘要" and saved["content"]==""
    assert saved["url"]==HIT["url"]


@pytest.mark.parametrize("planned",[None,{},[],{"query":"   "},RuntimeError("secret-token")])
def test_news_planning_failure_uses_real_search_fallback(services,planned):
    service=news_service(services,results=[dict(HIT)])
    service.llm.task_available.return_value=True
    if isinstance(planned,Exception):service.llm.generate_json.side_effect=planned
    else:service.llm.generate_json.return_value=planned
    assert asyncio.run(service.refresh_due(NOW,SCHEDULE,""))==1
    assert service.search.search.await_args.args[0]=="今日新闻 2026-11-05"
    assert "secret-token" not in str(service.logger.mock_calls)


def test_empty_search_does_not_record_reading_or_fetch_articles(services):
    service=news_service(services)
    assert asyncio.run(service.refresh_due(NOW,SCHEDULE,""))==0
    service.store.upsert_news_item.assert_not_awaited()
    service.search.fetch_article.assert_not_awaited()


def test_unchanged_news_is_not_counted_as_a_new_activity(services):
    service=news_service(services,results=[dict(HIT)])
    seen=set()
    async def save(item):
        key=(item["id"],item["content_hash"])
        changed=key not in seen
        seen.add(key)
        return changed
    service.store.upsert_news_item.side_effect=save
    assert asyncio.run(service.refresh_due(NOW,SCHEDULE,""))==1
    assert asyncio.run(service.refresh_due(NOW,SCHEDULE,""))==0


@pytest.mark.parametrize("blocked",["disabled","news_disabled","busy","daily_limit","host_unavailable"])
def test_news_preserves_execution_gates(services,blocked):
    service=news_service(services,results=[dict(HIT)])
    schedule=SCHEDULE
    if blocked=="disabled":service.config.network.enabled=False
    elif blocked=="news_disabled":service.config.network.news_enabled=False
    elif blocked=="busy":schedule={"current":{"kind":"sleep"}}
    elif blocked=="daily_limit":service.store.search_attempt_count.return_value=2
    else:service.search.available.return_value=False
    assert asyncio.run(service.refresh_due(NOW,schedule,""))==0
    service.search.search.assert_not_awaited()


def information_service(services):
    store=SimpleNamespace(recent_news_items=AsyncMock(return_value=[]),
        recent_exploration_notes=AsyncMock(return_value=[]),recent_search_history=AsyncMock(return_value=[]),
        search_attempt_count=AsyncMock(return_value=0),save_exploration_note=AsyncMock())
    service=services.information.InformationService(Mock(),store,config(),Mock(),Mock())
    service.search.available=Mock(return_value=True)
    service.search.sanitize_query=AsyncMock(side_effect=lambda value:value)
    service.search.search=AsyncMock(return_value={"results":[]})
    return service


def test_learning_context_carries_sources_and_distinguishes_snippet_from_article(services):
    service=information_service(services)
    service.store.recent_news_items.return_value=[
        {**HIT,"summary":"新闻摘要","relevance_score":0.5,"content":""},
        {**HIT,"summary":"另一篇摘要","relevance_score":0.5,"content":"正文独有的科学发现"+"文"*1500},
    ]
    service.store.recent_exploration_notes.return_value=[
        {"topic":"科学","summary":"检索笔记","relevance_score":0.6,"source_urls":[HIT["url"]]},
    ]
    context=asyncio.run(service.context(NOW))
    assert context["news"][0]["url"]==HIT["url"]
    assert [item["read_scope"] for item in context["news"]]==["snippet","article"]
    assert context["news"][0]["article_excerpt"]==""
    assert context["news"][1]["article_excerpt"].startswith("正文独有的科学发现")
    assert len(context["news"][1]["article_excerpt"])==1200
    assert context["explorations"][0]["source_urls"]==[HIT["url"]]


def test_status_uses_separate_search_and_news_switches(services):
    service=information_service(services)
    service.config.network.news_enabled=False
    status=asyncio.run(service.status(NOW))
    assert status["search_enabled"] is True and status["news_enabled"] is False


def test_learning_context_keeps_actual_low_relevance_sources_without_a_model(services):
    service=information_service(services)
    service.config.network.association_threshold=0.9
    service.store.recent_news_items.return_value=[
        {**HIT,"summary":"实际新闻摘要","relevance_score":0.2,"content":""},
    ]
    service.store.recent_exploration_notes.return_value=[
        {"topic":"科学","summary":"真实检索笔记","relevance_score":0.2,"source_urls":[HIT["url"]]},
    ]
    context=asyncio.run(service.context(NOW))
    assert context["news"][0]["summary"]=="实际新闻摘要"
    assert context["explorations"][0]["summary"]=="真实检索笔记"
    service.store.recent_news_items.assert_awaited_once_with(NOW.timestamp(),3,associated_only=True)
    service.store.recent_exploration_notes.assert_awaited_once_with(NOW.timestamp(),3)


@pytest.mark.parametrize("disabled",["network","limit"])
def test_learning_context_preserves_master_switch_and_budget(services,disabled):
    service=information_service(services)
    if disabled=="network":service.config.network.enabled=False
    else:service.config.network.context_item_limit=0
    assert asyncio.run(service.context(NOW))=={"news":[],"explorations":[]}
    service.store.recent_news_items.assert_not_awaited()
    service.store.recent_exploration_notes.assert_not_awaited()


def test_same_query_is_not_relearned_in_one_day(services):
    service=information_service(services)
    service._plan_query=AsyncMock(return_value={"topic":"科学","query":"科学新闻","reason":"了解"})
    note_id=hashlib.sha256(f"{NOW.date()}:科学新闻".encode()).hexdigest()
    service.store.recent_exploration_notes.return_value=[{"id":note_id}]
    assert asyncio.run(service.explore_due(NOW,"",{},SCHEDULE,[])) is False
    service.search.search.assert_not_awaited()
    service.store.save_exploration_note.assert_not_awaited()


def test_empty_exploration_does_not_create_learning_note(services):
    service=information_service(services)
    service._plan_query=AsyncMock(return_value={"topic":"科学","query":"科学新闻","reason":"了解"})
    assert asyncio.run(service.explore_due(NOW,"",{},SCHEDULE,[])) is False
    service.store.save_exploration_note.assert_not_awaited()


@pytest.fixture
def integrated_information(services,tmp_path):
    storage=importlib.import_module("mai_integration_tests.core.storage")
    settings=importlib.import_module("mai_integration_tests.config")
    cfg=settings.MaiLifeSettings(network={"enabled":True,"news_enabled":True,"news_daily_max":1,
        "news_full_text_count":1,"search_enabled":False,"max_attempts":1})
    store=storage.LifeStore(str(tmp_path),clock=lambda:NOW.timestamp())
    asyncio.run(store.initialize())
    web=SimpleNamespace(search=AsyncMock(),fetch=AsyncMock(return_value={"content":"正文独有知识：本次观测发现新卫星。"}))
    llm=SimpleNamespace(task_available=Mock(return_value=False),generate_json=AsyncMock())
    service=services.information.InformationService(SimpleNamespace(web=web),store,cfg,llm,Mock())
    try:yield service,web,llm
    finally:asyncio.run(store.close())


def test_real_tick_without_model_archives_cited_and_uncited_sources_and_respects_daily_quota(integrated_information):
    service,web,llm=integrated_information
    web.search.return_value={"service":"test","results":[dict(HIT),
        {"title":"无链接消息","snippet":"需要进一步核验的搜索摘要","url":""}]}
    async def exercise():
        assert await service.tick(NOW,"",{},SCHEDULE)=={"news":2,"associated":2,"search":0}
        context=await service.context(NOW)
        cited=next(item for item in context["news"] if item["url"])
        uncited=next(item for item in context["news"] if not item["url"])
        assert cited["url"]==HIT["url"] and cited["read_scope"]=="article"
        assert "本次观测发现新卫星" in cited["article_excerpt"]
        assert cited["summary"]==HIT["snippet"] and cited["score"]==0.2
        assert uncited["read_scope"]=="snippet" and uncited["article_excerpt"]==""
        assert "无外部链接" in uncited["summary"]
        assert await service.tick(NOW+timedelta(minutes=1),"",{},SCHEDULE)=={"news":0,"associated":0,"search":0}
        assert len(await service.store.recent_news_items(NOW.timestamp()))==2
        assert await service.store.pending_news_items(NOW.timestamp())==[]
        assert await service.store.active_opportunities(NOW.timestamp())==[]
        history=await service.store.recent_search_history(NOW.timestamp())
        assert len(history)==1 and history[0]["query"]=="今日新闻 2026-11-05"
    asyncio.run(exercise())
    web.search.assert_awaited_once()
    web.fetch.assert_awaited_once_with(HIT["url"])
    llm.generate_json.assert_not_awaited()


def test_real_tick_batches_news_and_archives_exploration_with_external_sharing_boundaries(integrated_information):
    service,web,llm=integrated_information
    service.config.network.search_enabled=True
    service.config.network.search_daily_max=1
    llm.task_available.return_value=True
    replies={
        "news_query_planning":{"query":"天文新闻"},
        "news_batch_digest":{"summary":"观测资料提到新卫星，结论仍待核验。"},
        "external_self_association":{"score":0.9,"reason":"与天文学习相关","share_topic":"天文观察","motive":"讨论观察方法"},
        "search_query_planning":{"topic":"天文学习","query":"行星观测基础","reason":"了解观察方法"},
        "search_digest":{"summary":"观测资料建议记录日期和方位。"},
    }
    llm.generate_json.side_effect=lambda *args,**kwargs:replies[kwargs["request_type"]]
    async def search(query,**kwargs):
        return {"service":"test","results":[dict(HIT) if query=="天文新闻" else
            {"title":"观察入门","url":"https://example.org/guide","snippet":"记录观察时间"}]}
    web.search.side_effect=search
    async def exercise():
        result=await service.tick(NOW,"喜欢天文",{},SCHEDULE)
        assert result=={"news":1,"associated":1,"search":1}
        context=await service.context(NOW)
        assert context["explorations"][0]["summary"]=="观测资料建议记录日期和方位。"
        assert context["explorations"][0]["source_urls"]==["https://example.org/guide"]
        assert "本次观测发现新卫星" in context["news"][0]["article_excerpt"]
        opportunities=await service.store.active_opportunities(NOW.timestamp())
        assert len(opportunities)==2
        assert all(item["privacy"]=="external" and "不可信" in item["motive"] for item in opportunities)
        assert await service.tick(NOW+timedelta(minutes=1),"喜欢天文",{},SCHEDULE)=={"news":0,"associated":0,"search":0}
        assert len(await service.store.active_opportunities(NOW.timestamp()))==2
    asyncio.run(exercise())
    requests=[call.kwargs["request_type"] for call in llm.generate_json.await_args_list]
    assert requests.count("news_batch_digest")==1
    batch=next(call for call in llm.generate_json.await_args_list if call.kwargs["request_type"]=="news_batch_digest")
    assert "不可信" in batch.args[0] and "本次观测发现新卫星" in batch.args[0]
    assert web.search.await_count==2


def test_real_tick_with_empty_results_records_attempt_but_no_learning_and_disabled_network_stays_offline(integrated_information):
    service,web,llm=integrated_information
    web.search.return_value={"service":"test","results":[]}
    async def exercise():
        assert await service.tick(NOW,"",{},SCHEDULE)=={"news":0,"associated":0,"search":0}
        assert await service.context(NOW)=={"news":[],"explorations":[]}
        history=await service.store.recent_search_history(NOW.timestamp())
        assert len(history)==1 and not history[0]["success"]
        assert await service.tick(NOW+timedelta(minutes=1),"",{},SCHEDULE)=={"news":0,"associated":0,"search":0}
        service.config.network.enabled=False
        assert await service.tick(NOW+timedelta(days=1),"",{},SCHEDULE)=={"news":0,"associated":0,"search":0}
        assert await service.store.active_opportunities(NOW.timestamp())==[]
    asyncio.run(exercise())
    web.search.assert_awaited_once()
    web.fetch.assert_not_awaited()
    llm.generate_json.assert_not_awaited()
