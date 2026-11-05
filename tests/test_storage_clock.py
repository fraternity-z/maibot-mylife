"""验证数据库和转述候选共用校准后的 epoch，而非服务器系统时钟。"""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo


def _load(name,path):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).parents[1]/path)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


storage=_load("mai_clock_storage","core/storage.py")
relay=_load("mai_clock_relay","social/relay_service.py")
search=_load("mai_clock_search","information/host_search.py")
continuity=_load("mai_clock_continuity","life/continuity.py")


class StorageClockTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.now=1_700_000_000.0
        self.clock=Mock(side_effect=lambda:self.now)
        self.store=storage.LifeStore(self.directory.name,clock=self.clock)
        await self.store.initialize()

    async def asyncTearDown(self):
        await self.store.close()
        self.directory.cleanup()

    async def test_initial_state_and_fallback_timestamps_use_clock(self):
        self.assertEqual((await self.store.get_state())["last_updated_at"],self.now)
        self.assertEqual((await self.store.get_sleep_runtime())["started_at"],self.now)
        await self.store.add_dream("dream",0,0,self.now-3600)
        self.assertEqual((await self.store.latest_dream())["created_at"],self.now)
        await self.store.cleanup_date_candidates(self.now-3600)
        row=self.store.conn.execute("SELECT last_cleanup_at FROM memory_runtime WHERE id=1").fetchone()
        self.assertEqual(row[0],self.now)

    async def test_recent_interactions_and_backlog_age_ignore_server_clock(self):
        await self.store.record_interaction("u","too old",self.now-7200,10)
        await self.store.record_interaction("u","recent",self.now-1800,11)
        await self.store.add_rest_backlog("u","a message",self.now-3600)
        with patch.object(storage.time,"time",return_value=self.now+8*3600):
            self.assertEqual(await self.store.recent_interactions("u",within_days=1/24),["recent"])
            self.assertEqual(await self.store.peek_rest_backlogs("u"),["约1小时前：a message"])
            self.assertEqual(await self.store.peek_rest_backlogs_batch(["u"]),{"u":["约1小时前：a message"]})
            self.assertEqual(await self.store.consume_rest_backlogs("u"),["约1小时前：a message"])
            self.assertEqual(await self.store.consume_rest_backlogs("u"),[])

    async def test_first_interaction_day_uses_shanghai_date_across_utc_midnight(self):
        instant=datetime(2026,11,5,20,30,tzinfo=timezone.utc)
        await self.store.record_interaction("u","message",instant.timestamp(),4)
        with patch.object(storage.time,"localtime",storage.time.gmtime):
            self.assertEqual(await self.store.first_interaction_day("u",timezone.utc),"2026-11-05")
            self.assertEqual(await self.store.first_interaction_day("u",ZoneInfo("Asia/Shanghai")),"2026-11-06")
            self.assertEqual(await self.store.first_interaction_day("missing",ZoneInfo("Asia/Shanghai")),"")
            self.assertEqual(await self.store.first_interaction_day("",ZoneInfo("Asia/Shanghai")),"")

    async def test_cleanup_counts_and_retains_days_in_shanghai_on_utc_server(self):
        now=datetime(2026,11,5,20,30,tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
        cutoff=now-timedelta(days=90)
        cutoff_day=cutoff.date().isoformat()
        previous_day=(cutoff.date()-timedelta(days=1)).isoformat()
        await self.store.add_proactive_pending("pending","u","op","stream",now.timestamp()-20,now.timestamp()-10)
        await self.store.record_proactive_skip("u","old",cutoff.timestamp()-86400,day=previous_day)
        await self.store.record_proactive_skip("u","keep",cutoff.timestamp(),day=cutoff_day)
        with patch.object(storage.time,"localtime",storage.time.gmtime):
            await self.store.cleanup_runtime_records(now.timestamp(),cutoff.timestamp(),
                records_before=cutoff.timestamp(),day=now.date().isoformat(),records_before_day=cutoff_day)
        rows=self.store.conn.execute("SELECT reason,day FROM proactive_skip_stats ORDER BY reason").fetchall()
        self.assertEqual([tuple(row) for row in rows],[("keep",cutoff_day),("planner_no_reply","2026-11-06")])

    async def test_relay_create_reserve_and_confirmation_use_same_epoch(self):
        ctx=SimpleNamespace(maisaka=SimpleNamespace(proactive=SimpleNamespace(
            trigger=AsyncMock(return_value={"success":True,"task_id":"host-task"}))))
        config=SimpleNamespace(group=SimpleNamespace(relay_enabled=True,relay_pending_seconds=60,
            profiles=[SimpleNamespace(enabled=True,group_id="123",relay_target_enabled=True)]))
        service=relay.RelayService(ctx,self.store,config,Mock(),clock=self.clock)
        service._resolve_group_stream=AsyncMock(return_value="group-stream")
        with patch.object(relay.time,"time",return_value=self.now+8*3600):
            result=await service.trigger_explicit("123","hello")
            self.assertTrue(result["success"])
            candidate=await self.store.relay_candidate(result["relay_id"])
            self.assertEqual(candidate["created_at"],self.now)
            self.assertEqual(candidate["expires_at"],self.now+60)
            self.assertIn("hello",await service.prompt_context("group-stream"))
            self.assertFalse(await service.should_abort_send({"session_id":"group-stream"},"host-task"))
            message,reserved=await service.mutate_before_send({"session_id":"group-stream"},"host-task")
            self.assertTrue(reserved)
            self.now+=5
            self.assertTrue(await service.confirm_after_send(message,True))
            candidate=await self.store.relay_candidate(result["relay_id"])
            self.assertEqual(candidate["status"],"sent")
            self.assertEqual(candidate["sent_at"],self.now)

    async def test_relay_expires_at_exact_calibrated_boundary(self):
        await self.store.create_relay_candidate({"id":"relay","kind":"explicit","target_stream_id":"stream",
            "created_at":self.now,"expires_at":self.now+60,"host_task_id":"task"})
        service=relay.RelayService(Mock(),self.store,Mock(),Mock(),clock=self.clock)
        self.now+=60
        self.assertTrue(await service.should_abort_send({"session_id":"stream"},"task"))
        self.assertEqual((await self.store.relay_candidate("relay"))["status"],"expired")


class ServiceClockTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_success_and_failure_record_calibrated_time(self):
        now=1_700_000_000.0
        store=Mock(clock=Mock(return_value=now),record_search_api_event=AsyncMock(),record_search_history=AsyncMock())
        web=SimpleNamespace(search=AsyncMock(return_value={"results":[{"title":"result","url":"https://example.com"}]}))
        config=SimpleNamespace(network=SimpleNamespace(max_attempts=1,history_enabled=True,history_retention_days=30))
        service=search.HostSearchService(SimpleNamespace(web=web),store,config,Mock())
        for failed in (False,True):
            web.search.side_effect=OSError("offline") if failed else None
            result=await service.search("query")
            self.assertEqual(bool(result["results"]),not failed)
            self.assertEqual(store.record_search_api_event.call_args.kwargs["created_at"],now)
            self.assertEqual(store.record_search_history.call_args.kwargs["created_at"],now)
            self.assertEqual(store.record_search_history.call_args.kwargs["expires_at"],now+30*86400)

    async def test_continuity_persists_and_throttles_with_calibrated_time(self):
        now=1_700_000_000.0
        store=Mock(clock=Mock(return_value=now),recent_interactions=AsyncMock(return_value=[]),
                   get_continuity=AsyncMock(return_value={"unresolved_topics":["topic"]}),save_continuity=AsyncMock())
        config=SimpleNamespace(context=SimpleNamespace(continuity_enabled=True,continuity_interval_minutes=10))
        service=continuity.ContinuityService(store,config,Mock(),Mock())
        await service.refresh("user","intent")
        await service.refresh("user","intent")
        store.save_continuity.assert_awaited_once_with("user","intent",["topic"],now)


if __name__=="__main__":unittest.main()
