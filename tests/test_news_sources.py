"""内置新闻源的离线验证：外部请求全部使用 Mock。"""
import asyncio
from datetime import datetime, timedelta, timezone
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


NOW=datetime(2026,11,5,12,tzinfo=timezone.utc)
SCHEDULE={"current":{"kind":"leisure"}}
RSS=b'''<rss version="2.0"><channel>
  <item><title>Fresh &amp; cited</title><link>https://example.org/first</link>
    <description>&lt;p&gt;Actual &amp;amp; useful&lt;/p&gt;</description>
    <pubDate>Thu, 05 Nov 2026 10:00:00 +0000</pubDate></item>
  <item><title>Another</title><link>/second</link><pubDate>2026-11-05T09:00:00Z</pubDate></item>
  <item><title>Stale</title><link>/old</link><pubDate>Sun, 01 Nov 2026 10:00:00 +0000</pubDate></item>
</channel></rss>'''


@pytest.fixture
def modules(plugin_module):
    return SimpleNamespace(**{name:importlib.import_module("mai_integration_tests."+path)
        for name,path in {"news":"information.news_service","host":"information.host_search",
                          "information":"information.information_service","http":"information.http_client",
                          "storage":"core.storage","config":"config"}.items()})


@pytest.fixture
def service(modules,tmp_path):
    cfg=modules.config.MaiLifeSettings(network={"enabled":True,"news_enabled":True,
        "news_daily_max":1,"max_attempts":1,"news_full_text_count":0,"news_sources":["ithome"]})
    store=modules.storage.LifeStore(str(tmp_path),clock=lambda:NOW.timestamp())
    asyncio.run(store.initialize())
    search=modules.host.HostSearchService(SimpleNamespace(web=None),store,cfg,Mock())
    llm=SimpleNamespace(task_available=Mock(return_value=False),generate_json=AsyncMock())
    news=modules.news.NewsService(store,cfg,search,Mock(),llm)
    news.http.get=AsyncMock(return_value=modules.http.HttpResponse(200,"https://example.org/rss",{},RSS))
    try:yield news
    finally:asyncio.run(store.close())


def test_default_sources_are_selectable_and_separately_instantiated(modules):
    left=modules.config.MaiLifeSettings(); right=modules.config.MaiLifeSettings()
    assert left.network.news_sources==["ithome","solidot","sspai"]
    left.network.news_sources.clear()
    assert right.network.news_sources==["ithome","solidot","sspai"]
    with pytest.raises(ValueError):modules.config.MaiLifeSettings(network={"news_sources":["unknown"]})


def test_source_status_lists_selected_feeds_without_duplicate_counts(service,modules):
    service.config.network.news_sources=["ithome","solidot","ithome"]
    information=modules.information.InformationService(SimpleNamespace(web=None),service.store,
                                                       service.config,service.llm,Mock())
    status=asyncio.run(information.status(NOW))
    assert status["sources"]==2
    assert status["news_sources"]==[
        {"id":"ithome","name":"IT之家","url":"https://www.ithome.com/rss/"},
        {"id":"solidot","name":"Solidot","url":"https://www.solidot.org/index.rss"},
    ]


def test_rss_works_without_host_or_model_and_obeys_persisted_quota(service):
    async def exercise():
        assert await service.refresh_due(NOW,SCHEDULE,"")==2
        rows=await service.store.recent_news_items(NOW.timestamp())
        first=next(item for item in rows if item["url"].endswith("/first"))
        assert first["source_id"]=="rss:ithome" and first["summary"]=="Actual & useful"
        assert first["published_at"]==NOW.replace(hour=10).timestamp()
        assert first["content"]==""
        assert await service.store.search_attempt_count("news",*service._day_bounds(NOW))==1
        history=await service.store.recent_search_history(NOW.timestamp())
        assert len(history)==1 and history[0]["provider_type"]=="rss"
        assert await service.refresh_due(NOW+timedelta(minutes=1),SCHEDULE,"")==0
        restarted=type(service)(service.store,service.config,service.search,Mock(),service.llm)
        restarted.http.get=AsyncMock()
        assert await restarted.refresh_due(NOW+timedelta(minutes=2),SCHEDULE,"")==0
        restarted.http.get.assert_not_awaited()
    asyncio.run(exercise())
    service.http.get.assert_awaited_once_with("https://www.ithome.com/rss/",timeout=8,
                                            max_bytes=1_000_000,public_only=True)
    service.llm.generate_json.assert_not_awaited()


@pytest.mark.parametrize("gate",["network","news","quota","schedule"])
def test_feeds_obey_execution_gates(service,gate):
    schedule=SCHEDULE
    if gate=="network":service.config.network.enabled=False
    elif gate=="news":service.config.network.news_enabled=False
    elif gate=="quota":service.config.network.news_daily_max=0
    else:schedule={"current":{"kind":"sleep"}}
    assert asyncio.run(service.refresh_due(NOW,schedule,""))==0
    service.http.get.assert_not_awaited()


@pytest.mark.parametrize("failure",[RuntimeError("secret-token"),None])
def test_failed_or_empty_feed_consumes_quota_without_leaking_errors(service,modules,failure):
    if failure:service.http.get.side_effect=failure
    else:service.http.get.return_value=modules.http.HttpResponse(200,"https://example.org/",{},b"<rss><channel/></rss>")
    async def exercise():
        assert await service.refresh_due(NOW,SCHEDULE,"")==0
        assert await service.refresh_due(NOW+timedelta(minutes=1),SCHEDULE,"")==0
        assert await service.store.search_attempt_count("news",*service._day_bounds(NOW))==1
        history=await service.store.recent_search_history(NOW.timestamp())
        assert len(history)==1 and history[0]["error_class"]=="feed_unavailable"
    asyncio.run(exercise())
    service.http.get.assert_awaited_once()
    assert "secret-token" not in str(service.logger.mock_calls)


def test_failed_feeds_fall_back_to_host_as_one_read(service):
    web=SimpleNamespace(search=AsyncMock(return_value={"service":"test","results":[
        {"title":"Host result","url":"https://example.org/host","snippet":"Verified search"}]}))
    service.search.ctx.web=web
    service.http.get.side_effect=OSError("unavailable")
    async def exercise():
        assert await service.refresh_due(NOW,SCHEDULE,"")==1
        assert await service.store.search_attempt_count("news",*service._day_bounds(NOW))==1
        assert len(await service.store.recent_search_history(NOW.timestamp()))==1
    asyncio.run(exercise())
    web.search.assert_awaited_once()


def test_failed_article_fetch_keeps_feed_summary_without_logging_secret(service):
    service.config.network.news_full_text_count=1
    web=SimpleNamespace(fetch=AsyncMock(side_effect=RuntimeError("secret-token")),search=AsyncMock())
    service.search.ctx.web=web
    assert asyncio.run(service.refresh_due(NOW,SCHEDULE,""))==2
    rows=asyncio.run(service.store.recent_news_items(NOW.timestamp()))
    assert next(item for item in rows if item["url"].endswith("/first"))["summary"]=="Actual & useful"
    assert all(item["content"]=="" for item in rows)
    web.fetch.assert_awaited_once()
    web.search.assert_not_awaited()
    assert "secret-token" not in str(service.search.logger.mock_calls)


def test_failed_feed_still_counts_when_privacy_filter_blocks_fallback_query(service):
    service.http.get.side_effect=OSError("unavailable")
    service.search.ctx.web=SimpleNamespace(search=AsyncMock())
    service.search.sanitize_query=AsyncMock(return_value="")
    async def exercise():
        assert await service.refresh_due(NOW,SCHEDULE,"")==0
        assert await service.store.search_attempt_count("news",*service._day_bounds(NOW))==1
    asyncio.run(exercise())
    service.search.ctx.web.search.assert_not_awaited()


def test_empty_source_selection_keeps_host_search(service):
    service.config.network.news_sources=[]
    web=SimpleNamespace(search=AsyncMock(return_value={"results":[]}))
    service.search.ctx.web=web
    assert asyncio.run(service.refresh_due(NOW,SCHEDULE,""))==0
    service.http.get.assert_not_awaited()
    web.search.assert_awaited_once()


def test_duplicate_news_is_not_new_learning_and_concurrent_refresh_obeys_quota(service):
    async def exercise():
        assert sorted(await asyncio.gather(service.refresh_due(NOW,SCHEDULE,""),
                                           service.refresh_due(NOW,SCHEDULE,"")))==[0,2]
        assert await service.refresh_due(NOW+timedelta(days=1),SCHEDULE,"")==0
        assert len(await service.store.recent_news_items(NOW.timestamp()))==2
    asyncio.run(exercise())
    assert service.http.get.await_count==2


def test_multiple_sources_share_result_budget_and_failure_isolated(service,modules):
    service.config.network.news_sources=["ithome","solidot","sspai"]
    service.config.network.max_results=2
    def response(url,**kwargs):
        if "sspai" in url:raise OSError("one unavailable source")
        body=RSS if "ithome" in url else RSS.replace(b"/first",b"/solidot").replace(b"/second",b"/solidot2")
        return modules.http.HttpResponse(200,"https://example.org/",{},body)
    service.http.get.side_effect=response
    async def exercise():
        assert await service.refresh_due(NOW,SCHEDULE,"")==2
        rows=await service.store.recent_news_items(NOW.timestamp())
        assert {row["source_id"] for row in rows}=={"rss:ithome","rss:solidot"}
    asyncio.run(exercise())


def test_atom_extracts_alternate_link_summary_and_date(modules):
    xml=b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <title>Atom news</title><link rel="self" href="https://example.org/xml"/>
      <link rel="alternate" href="/article"/><summary type="html">&lt;b&gt;News&lt;/b&gt;</summary>
      <updated>2026-11-05T11:00:00Z</updated></entry></feed>'''
    rows=modules.news.NewsService._parse_feed(xml,"https://example.org/feed","solidot")
    assert len(rows)==1 and rows[0]["url"]=="https://example.org/article"
    assert rows[0]["snippet"]=="News" and rows[0]["published_at"]=="2026-11-05T11:00:00Z"


@pytest.mark.parametrize("xml",[
    b'<!DOCTYPE rss [<!ENTITY x "expanded">]><rss><channel/></rss>',
    '<!DOCTYPE rss [<!ENTITY x "expanded">]><rss><channel/></rss>'.encode("utf-16"),
    b"<rss>broken",
])
def test_unsafe_or_malformed_xml_is_rejected(modules,xml):
    with pytest.raises((ValueError,modules.news.ElementTree.ParseError)):
        modules.news.NewsService._parse_feed(xml,"https://example.org/feed","ithome")


@pytest.mark.parametrize("link",["javascript:alert(1)","http://127.0.0.1/a",
    "http://169.254.169.254/latest", "http://name:secret@example.org/a","http://router.local/a",""])
def test_feed_drops_unsafe_or_missing_links(modules,link):
    xml=f"<rss><channel><item><title>Unsafe</title><link>{link}</link></item></channel></rss>".encode()
    assert modules.news.NewsService._parse_feed(xml,"https://example.org/feed","ithome")==[]


@pytest.mark.parametrize("published",["2026-11-01T00:00:00Z","Sun, 01 Nov 2026 00:00:00 GMT",1793491200])
def test_stale_news_is_rejected_for_all_supported_dates(modules,published):
    assert modules.news.NewsService._recent_results([{"published_at":published}],NOW.timestamp())==[]


@pytest.mark.parametrize("published",["",None,"unparseable","nan","inf"])
def test_unknown_dates_do_not_break_reading(modules,published):
    row={"published_at":published}
    assert modules.news.NewsService._recent_results([row],NOW.timestamp())==[row]
