"""验证固定活动窗口、实际执行记录和旧库兼容。"""
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
        self.config=SimpleNamespace()
        self.llm=Mock(task_available=Mock(return_value=False),generate_json=AsyncMock(side_effect=RuntimeError("offline")))
        self.service=schedule.ScheduleService(self.store,self.config,self.llm,self.directory.name,Mock())

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

    async def test_legacy_generated_candidates_are_preserved_but_only_actual_activity_is_selected(self):
        for kind in ("dream","scene"):
            await self.store.add_opportunity({"id":kind,"framework_id":f"{kind}:1","topic":kind,
                "motive":"分享","weight":0.8,"expires_at":NOW.timestamp()+3600})
        for kind in ("dream","life","scene","activity"):
            await self.store.add_creation_inspiration({"id":kind,"source_kind":kind,"source_ref":"1",
                "prompt_digest":kind,"score":0.9 if kind=="dream" else 0.5,
                "created_at":NOW.timestamp(),"expires_at":NOW.timestamp()+3600})
        self.assertEqual([row["id"] for row in await self.store.active_opportunities(NOW.timestamp())],["scene"])
        self.assertEqual([row["id"] for row in await self.store.pending_creation_inspirations(NOW.timestamp(),limit=1)],["activity"])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM proactive_opportunities").fetchone()[0],2)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM creation_inspirations").fetchone()[0],4)

    async def test_scene_and_opportunities_roll_back_together(self):
        await self.store.save_scene("node","原场景",[])
        with self.assertRaises(KeyError):await self.store.save_scene("node","错误场景",[{"id":"bad"}])
        self.assertEqual((await self.store.get_scene("node"))["scene"],"原场景")
        self.assertEqual(await self.store.active_opportunities(NOW.timestamp()),[])

    async def test_fixed_windows_cover_the_day_without_personality_weather_or_model_generation(self):
        with patch.object(self.store,"get_state",side_effect=AssertionError("日程不再读取生理状态")):
            nodes=await self.service.ensure_day(NOW,"大学学生","晴天")
        self.assertEqual(nodes[0]["start_minute"],0)
        self.assertEqual(nodes[-1]["end_minute"],1440)
        self.assertTrue(all(left["end_minute"]==right["start_minute"] for left,right in zip(nodes,nodes[1:])))
        self.assertEqual({node["kind"] for node in nodes},{"sleep","leisure","study"})
        self.assertTrue(all(node["location"]=="在线" and node["shareability"]==0 for node in nodes))
        self.assertTrue(all(node["id"].startswith("task:") for node in nodes))
        self.assertTrue(all("energy_load" not in node for node in nodes))
        self.assertEqual(await self.service.ensure_day(NOW,"新身份","雨天"),nodes)
        weekend=await self.service.ensure_day(NOW+timedelta(days=2),"职场人士","暴雨",
            memory_context={"private_date_hints":[{"kind":"生日","days":1}]},environment={"holiday":"节日"})
        keys=("start_minute","end_minute","kind","summary","location")
        self.assertEqual([[node[key] for key in keys] for node in nodes],
                         [[node[key] for key in keys] for node in weekend])
        self.llm.task_available.assert_not_called()
        self.llm.generate_json.assert_not_awaited()

    async def test_old_current_framework_is_migrated_without_using_its_generated_scene(self):
        previous=(NOW-timedelta(days=1)).date().isoformat()
        for day,node_id in ((DAY,"old-today"),(previous,"old-yesterday")):
            await self.store.replace_framework(day,[{"id":node_id,"day":day,"start_minute":0,
                "end_minute":1440,"kind":"meal","summary":"在厨房做饭","location":"厨房","shareability":0.5}])
            await self.store.save_scene(node_id,"随机生成的厨房场景",[])
        context=await self.service.context(NOW)
        self.assertEqual(context,{"nodes":[],"current":None,"next":None,"scene":{}})
        nodes=await self.service.ensure_day(NOW)
        self.assertTrue(all(node["id"].startswith("task:") for node in nodes))
        self.assertEqual((await self.service.context(NOW))["scene"],{})
        self.assertEqual(await self.store.get_scene("old-today"),{})
        self.assertEqual((await self.store.get_scene("old-yesterday"))["scene"],"随机生成的厨房场景")
        self.llm.generate_json.assert_not_awaited()

    async def test_only_recorded_completed_activity_creates_a_scene(self):
        nodes=await self.service.ensure_day(NOW,"普通网友","晴天")
        context=await self.service.context(NOW)
        self.assertEqual(context["scene"],{})
        self.assertTrue(all(not row for row in (await self.store.get_scenes_by_framework_ids([node["id"] for node in nodes])).values()))
        await self.service.record_activity(NOW,"已检索并保存2条新闻摘要")
        await self.service.record_activity(NOW,"已检索并保存2条新闻摘要")
        await self.service.record_activity(NOW,"已保存1篇主题检索笔记")
        context=await self.service.context(NOW)
        self.assertEqual(context["scene"]["scene"],"已检索并保存2条新闻摘要\n已保存1篇主题检索笔记")
        self.assertEqual(set(context["scene"]),{"framework_id","scene","created_at"})
        self.assertEqual(await self.store.get_scene(context["next"]["id"]),{})
        self.assertEqual(await self.store.active_opportunities(NOW.timestamp()),[])
        scenes=await self.store.get_scenes_by_framework_ids([context["current"]["id"],"missing"])
        self.assertEqual(scenes[context["current"]["id"]],context["scene"])
        self.assertEqual(scenes["missing"],{})
        self.assertEqual((await self.store.scenes_for_day(DAY))[3]["scene"],context["scene"]["scene"])
        self.llm.generate_json.assert_not_awaited()

    async def test_force_keeps_completed_activity_and_does_not_rebuild_fixed_windows(self):
        nodes=await self.service.ensure_day(NOW)
        await self.service.record_activity(NOW,"已检索并保存2条新闻摘要")
        before=await self.service.context(NOW)
        with patch.object(self.store,"replace_framework",wraps=self.store.replace_framework) as replace:
            self.assertEqual(await self.service.ensure_day(NOW,"新身份","雨天",force=True),nodes)
            replace.assert_not_awaited()
        self.assertEqual(await self.service.context(NOW),before)

    async def test_standby_empty_summary_and_missing_framework_never_record_completion(self):
        nodes=await self.service.ensure_day(NOW)
        await self.service.record_activity(NOW.replace(hour=3),"夜间不应记录完成")
        await self.service.record_activity(NOW.replace(hour=23,minute=45),"夜间不应记录完成")
        await self.service.record_activity(NOW," \n ")
        await self.service.record_activity(NOW+timedelta(days=1),"未安排活动不记录")
        self.assertTrue(all(not row for row in (await self.store.get_scenes_by_framework_ids([node["id"] for node in nodes])).values()))
        self.assertEqual(await self.store.active_opportunities(NOW.timestamp()),[])

    async def test_current_and_next_window_boundaries_and_time_helpers(self):
        nodes=await self.service.ensure_day(NOW)
        for index,node in enumerate(nodes):
            with self.subTest(start=node["start_minute"]):
                current,nxt=self.service.current_and_next(nodes,node["start_minute"])
                self.assertEqual(current,node)
                self.assertEqual(nxt,nodes[index+1] if index+1<len(nodes) else None)
                self.assertEqual(self.service.current_and_next(nodes,node["end_minute"]-1)[0],node)
        self.assertEqual(self.service.current_and_next(nodes,1440),(None,None))
        for invalid in ("25:00","12:60","bad","24:00"):
            self.assertIsNone(schedule.to_minute(invalid))
        self.assertEqual(schedule.to_minute("24:00",allow_day_end=True),1440)
        self.assertEqual(schedule.to_minute("08:30"),510)
        self.assertEqual(schedule.hhmm(-1),"00:00")
        self.assertEqual(schedule.hhmm(1440),"24:00")


if __name__=="__main__":unittest.main()
