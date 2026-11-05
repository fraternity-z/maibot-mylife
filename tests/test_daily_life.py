"""验证精简生活数据、旧库兼容和普通日程场景。"""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


def _load(name,path):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).parents[1]/path)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


storage=_load("mai_daily_storage","core/storage.py")
schedule=_load("mai_daily_schedule","life/schedule_service.py")
NOW=datetime(2026,11,5,12,tzinfo=timezone(timedelta(hours=8)))
DAY=NOW.date().isoformat()
LEGACY_SCHEMA="""
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE global_state(
  id INTEGER PRIMARY KEY CHECK(id=1),energy REAL NOT NULL,hunger REAL NOT NULL,
  mood_valence REAL NOT NULL,health_status TEXT NOT NULL,health_note TEXT NOT NULL,
  sleep_phase TEXT NOT NULL,current_location TEXT NOT NULL,current_activity TEXT NOT NULL,
  last_updated_at REAL NOT NULL
);
CREATE TABLE daily_framework(
  id TEXT PRIMARY KEY,day TEXT NOT NULL,start_minute INTEGER NOT NULL,end_minute INTEGER NOT NULL,
  kind TEXT NOT NULL,summary TEXT NOT NULL,location TEXT NOT NULL,energy_load REAL NOT NULL,
  shareability REAL NOT NULL
);
CREATE TABLE detailed_scenes(
  framework_id TEXT PRIMARY KEY,scene TEXT NOT NULL,state_deltas TEXT NOT NULL,
  created_at REAL NOT NULL,applied INTEGER NOT NULL DEFAULT 0
);
"""


class DailyLifeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.store=storage.LifeStore(self.directory.name,clock=lambda:NOW.timestamp())
        await self.store.initialize()
        self.config=SimpleNamespace(schedule=SimpleNamespace(template_file="missing-template.json",detail_lead_minutes=30))
        self.llm=Mock(task_available=Mock(return_value=False),generate_json=AsyncMock(side_effect=RuntimeError("offline")))
        self.service=schedule.ScheduleService(self.store,self.config,self.llm,self.directory.name,Mock())
        self.service.retry_attempts=1

    async def asyncTearDown(self):
        await self.store.close()
        self.directory.cleanup()

    async def test_new_store_only_has_daily_activity_state(self):
        self.assertEqual(await self.store.get_state(),{
            "current_location":"家里","current_activity":"自由活动","last_updated_at":NOW.timestamp()})
        columns={row[1] for row in self.store.conn.execute("PRAGMA table_info(global_state)")}
        self.assertEqual(columns,{"id","current_location","current_activity","last_updated_at"})
        tables={row[0] for row in self.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse(tables.intersection({"sleep_runtime","dreams","dream_fragments","wake_candidates",
                                             "mood_events","state_snapshots","recall_events","rest_backlogs"}))
        for table,removed in (("daily_framework",{"energy_load"}),("detailed_scenes",{"state_deltas","applied"})):
            self.assertFalse({row[1] for row in self.store.conn.execute(f"PRAGMA table_info({table})")}.intersection(removed))
        updated={"current_location":"图书馆","current_activity":"整理笔记","last_updated_at":NOW.timestamp()+60}
        await self.store.save_state(updated)
        self.assertEqual(await self.store.get_state(),updated)
        with self.assertRaises(KeyError):await self.store.save_state({"current_location":"公园"})
        self.assertEqual(await self.store.get_state(),updated)

    async def test_old_store_keeps_history_and_accepts_daily_writes(self):
        for version,has_state in ((1,False),(12,True),(14,False),(14,True)):
            with self.subTest(version=version,has_state=has_state),tempfile.TemporaryDirectory() as directory:
                database=Path(directory)/"mai_life.db"
                conn=sqlite3.connect(database)
                conn.executescript(LEGACY_SCHEMA)
                conn.execute("INSERT INTO meta VALUES('schema_version',?)",(str(version),))
                if has_state:
                    conn.execute("INSERT INTO global_state VALUES(1,42,23,0.2,'normal','旧健康记录','awake','学校','旧活动',?)",(NOW.timestamp()-60,))
                history=("sleep_runtime","dreams","dream_fragments","wake_candidates","mood_events","state_snapshots","recall_events","rest_backlogs")
                for table in history:
                    conn.execute(f"CREATE TABLE {table}(payload TEXT NOT NULL)")
                    conn.execute(f"INSERT INTO {table} VALUES('历史数据')")
                conn.commit(); conn.close()
                store=storage.LifeStore(directory,clock=lambda:NOW.timestamp())
                try:
                    await store.initialize()
                    state=await store.get_state()
                    self.assertEqual(set(state),{"current_location","current_activity","last_updated_at"})
                    self.assertEqual(state["current_activity"],"旧活动" if has_state else "自由活动")
                    await store.save_state({"current_location":"公园","current_activity":"散步","last_updated_at":NOW.timestamp()})
                    self.assertEqual((await store.get_state())["current_activity"],"散步")
                    if has_state:
                        self.assertEqual(tuple(store.conn.execute("SELECT energy,health_note FROM global_state").fetchone()),(42,"旧健康记录"))
                    node={"id":"node","day":DAY,"start_minute":720,"end_minute":780,"kind":"meal",
                          "summary":"准备午饭","location":"厨房","shareability":0.5}
                    await store.replace_framework(DAY,[node])
                    await store.save_scene("node","切菜准备午饭",[])
                    self.assertEqual(await store.get_framework(DAY),[node])
                    self.assertEqual(set(await store.get_scene("node")),{"framework_id","scene","created_at"})
                    self.assertEqual(store.conn.execute("SELECT energy_load FROM daily_framework").fetchone()[0],0)
                    self.assertEqual(store.conn.execute("SELECT state_deltas FROM detailed_scenes").fetchone()[0],"{}")
                    await store.save_diary(DAY,"日常","散步后回家做饭。","平常的一天","digest",NOW.timestamp())
                    await store.cleanup_runtime_records(NOW.timestamp(),0,records_before=NOW.timestamp()-90*86400,day=DAY)
                    await store.initialize()
                    for table in history:
                        self.assertEqual(store.conn.execute(f"SELECT payload FROM {table}").fetchone()[0],"历史数据")
                    self.assertEqual((await store.get_diary(DAY))["content"],"散步后回家做饭。")
                    self.assertFalse(list(Path(directory).glob("*.incompatible.*")))
                finally:await store.close()

    async def test_legacy_dream_candidates_are_preserved_but_not_selected(self):
        for kind in ("dream","scene"):
            await self.store.add_opportunity({"id":kind,"framework_id":f"{kind}:1","topic":kind,
                "motive":"分享","weight":0.8,"expires_at":NOW.timestamp()+3600})
            await self.store.add_creation_inspiration({"id":kind,"source_kind":kind,"source_ref":"1",
                "prompt_digest":kind,"score":0.9 if kind=="dream" else 0.5,
                "created_at":NOW.timestamp(),"expires_at":NOW.timestamp()+3600})
        self.assertEqual([row["id"] for row in await self.store.active_opportunities(NOW.timestamp())],["scene"])
        self.assertEqual([row["id"] for row in await self.store.pending_creation_inspirations(NOW.timestamp(),limit=1)],["scene"])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM proactive_opportunities").fetchone()[0],2)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM creation_inspirations").fetchone()[0],2)

    async def test_scene_and_opportunities_roll_back_together(self):
        await self.store.save_scene("node","原场景",[])
        with self.assertRaises(KeyError):await self.store.save_scene("node","错误场景",[{"id":"bad"}])
        self.assertEqual((await self.store.get_scene("node"))["scene"],"原场景")
        self.assertEqual(await self.store.active_opportunities(NOW.timestamp()),[])

    async def test_fallback_schedule_keeps_daily_routines_and_identity(self):
        with patch.object(self.store,"get_state",side_effect=AssertionError("日程不再读取生理状态")):
            nodes=await self.service.ensure_day(NOW,"大学学生","晴天")
        self.assertGreaterEqual(sum(node["kind"]=="meal" for node in nodes),2)
        self.assertTrue(any(node["kind"]=="sleep" for node in nodes))
        self.assertTrue(any(node["location"]=="学校" for node in nodes))
        self.assertTrue(all("energy_load" not in node for node in nodes))
        self.assertEqual(await self.service.ensure_day(NOW,"新身份","雨天"),nodes)
        self.llm.generate_json.assert_not_awaited()

    async def test_schedule_generation_ignores_legacy_factors_and_sanitizes_template(self):
        raw=[{"start":"00:00","end":"08:00","kind":"sleep","summary":"睡觉"},
             {"start":"08:00","end":"09:00","kind":"meal","summary":"吃早饭"},
             {"start":"12:00","end":"13:00","kind":"meal","summary":"吃午饭"}]
        for item in raw:item.update(energy_load=999,state_deltas={"hunger":-90})
        self.llm.task_available.return_value=True
        self.llm.generate_json.side_effect=None; self.llm.generate_json.return_value=raw
        with patch.object(self.service,"_template",return_value={"workday":raw}):
            nodes=await self.service.ensure_day(NOW,"普通网友","晴天",force=True)
        self.assertEqual(len(nodes),3)
        self.assertTrue(all("energy_load" not in node and "state_deltas" not in node for node in nodes))
        prompt=self.llm.generate_json.await_args.args[0]
        for term in ("energy_load","state_deltas","hunger","精力","饥饿"):self.assertNotIn(term,prompt)

    async def test_invalid_schedule_generation_falls_back(self):
        self.llm.task_available.return_value=True
        self.llm.generate_json.side_effect=None; self.llm.generate_json.return_value=[{"start":"12:00","end":"13:00","kind":"work"}]
        nodes=await self.service.ensure_day(NOW,"普通网友","晴天")
        self.assertTrue(any(node["kind"]=="sleep" for node in nodes))
        self.assertGreaterEqual(sum(node["kind"]=="meal" for node in nodes),2)

    async def test_expand_scene_produces_daily_context_and_single_share_opportunity(self):
        nodes=await self.service.ensure_day(NOW,"普通网友","晴天")
        self.llm.generate_json.side_effect=None
        self.llm.generate_json.return_value={"scene":"正在厨房洗菜，准备午饭。","opportunities":[{
            "topic":"今天的午饭","motive":"想聊聊新菜谱","weight":0.6}]}
        await self.service.expand_due(NOW,nodes,"晴天")
        context=await self.service.context(NOW)
        self.assertEqual(context["current"]["kind"],"meal")
        self.assertEqual(context["next"]["kind"],"nap")
        self.assertEqual(context["scene"]["scene"],"正在厨房洗菜，准备午饭。")
        self.assertEqual(set(context["scene"]),{"framework_id","scene","created_at"})
        self.assertEqual(len(await self.store.active_opportunities(NOW.timestamp())),1)
        await self.service.expand_due(NOW,nodes,"晴天")
        self.llm.generate_json.assert_awaited_once()
        scenes=await self.store.get_scenes_by_framework_ids([context["current"]["id"],"missing"])
        self.assertEqual(scenes[context["current"]["id"]],context["scene"])
        self.assertEqual(scenes["missing"],{})
        self.assertEqual((await self.store.scenes_for_day(DAY))[3]["scene"],context["scene"]["scene"])
        await self.store.replace_framework(DAY,nodes)
        self.assertEqual(await self.store.active_opportunities(NOW.timestamp()),[])
        self.assertEqual(await self.store.get_scene(context["current"]["id"]),{})

    async def test_scene_generation_failure_uses_plain_activity(self):
        nodes=await self.service.ensure_day(NOW,"普通网友","晴天")
        await self.service.expand_due(NOW,nodes,"晴天")
        context=await self.service.context(NOW)
        self.assertEqual(context["scene"]["scene"],context["current"]["summary"])

    def test_validation_rejects_invalid_times_and_repairs_overlap(self):
        self.assertEqual(self.service._validate(DAY,{}),[])
        self.assertEqual(self.service._validate(DAY,[{"start":"25:00","end":"26:00","kind":"sleep"}]),[])
        raw=[{"start":"00:00","end":"08:00","kind":"sleep"},
             {"start":"07:00","end":"09:00","kind":"meal","shareability":"invalid"},
             {"start":"12:00","end":"13:00","kind":"meal"}]
        nodes=self.service._validate(DAY,raw)
        self.assertEqual(nodes[1]["start_minute"],480)
        self.assertEqual(nodes[1]["shareability"],0.3)


if __name__=="__main__":unittest.main()
