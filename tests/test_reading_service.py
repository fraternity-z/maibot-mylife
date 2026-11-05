"""真实小说分段阅读、私人笔记及原子进度的离线回归。"""
import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


def _load(name,path):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).parents[1]/path)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


storage=_load("mai_reading_storage","core/storage.py")
reading=_load("mai_reading_service","information/reading_service.py")
NOW=datetime(2026,11,5,12,tzinfo=timezone(timedelta(hours=8)))
STUDY={"current":{"kind":"study"}}


class ReadingServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory(); self.root=Path(self.directory.name)
        self.novels=self.root/"novels"; self.novels.mkdir()
        self.store=storage.LifeStore(str(self.root/"data"),clock=lambda:NOW.timestamp())
        await self.store.initialize()
        self.config=SimpleNamespace(reading=SimpleNamespace(enabled=True,directory="novels",chunk_chars=4,
                                                           daily_max=4,interval_minutes=60))
        self.llm=Mock(task_available=Mock(return_value=False),generate=AsyncMock())
        self.logger=Mock()
        self.service=self.make_service()

    def make_service(self):
        return reading.ReadingService(self.store,self.config,self.llm,str(self.root),self.logger)

    def book(self,name="私人小说.txt",text="第一章。第二章。第三章。"):
        path=self.novels/name; path.write_text(text,encoding="utf-8"); return path

    async def asyncTearDown(self):
        await self.store.close(); self.directory.cleanup()

    async def test_reads_actual_segment_and_preserves_private_evidence(self):
        self.book()
        result=await self.service.tick(NOW,STUDY)
        self.assertEqual(result,{"status":"read","summary":"已阅读小说片段4字并保存私人笔记","chars":4,"completed":False})
        self.assertNotIn("私人小说",result["summary"])
        self.assertNotIn("第一章",result["summary"])
        rows=await self.store.recent_local_reading_notes()
        self.assertEqual((rows[0]["source_path"],rows[0]["start_offset"],rows[0]["end_offset"],rows[0]["excerpt"]),
                         ("novels/私人小说.txt",0,4,"第一章。"))
        self.assertIn("未生成批注",rows[0]["annotation"])
        self.assertEqual(await self.store.reading_progress(rows[0]["source_digest"]),4)
        self.assertEqual(await self.store.list_bookshelf_documents(allow_private=False),[])
        shelf=await self.store.list_bookshelf_documents(allow_private=True)
        self.assertEqual(shelf[0]["privacy"],"private")
        self.assertIn("字符范围：1-4/12",shelf[0]["summary"])
        self.llm.generate.assert_not_awaited()

    async def test_resume_after_restart_across_days_and_never_repeat_finished_book(self):
        self.book(text="第一章。第二章。")
        await self.service.tick(NOW,STUDY)
        await self.store.close(); await self.store.initialize()
        result=await self.make_service().tick(NOW+timedelta(days=1),STUDY)
        self.assertTrue(result["completed"])
        rows=await self.store.recent_local_reading_notes()
        self.assertEqual(rows[0]["excerpt"],"第二章。")
        self.assertEqual(await self.make_service().tick(NOW+timedelta(days=2),STUDY),{"status":"completed"})
        self.assertEqual(len(await self.store.recent_local_reading_notes()),2)

    async def test_stable_order_content_digest_and_changed_sources(self):
        second=self.book("b.txt","乙乙乙乙")
        first=self.book("a.txt","甲甲甲甲")
        await self.service.tick(NOW,STUDY)
        first.rename(self.novels/"A-renamed.txt")
        result=await self.service.tick(NOW+timedelta(hours=1),STUDY)
        self.assertTrue(result["completed"])
        rows=await self.store.recent_local_reading_notes()
        self.assertEqual([item["excerpt"] for item in rows],["乙乙乙乙","甲甲甲甲"])
        second.write_text("丙丙丙丙",encoding="utf-8")
        await self.service.tick(NOW+timedelta(hours=2),STUDY)
        self.assertEqual((await self.store.recent_local_reading_notes())[0]["excerpt"],"丙丙丙丙")

    async def test_daily_and_interval_limits_survive_new_service(self):
        self.book(text="一二三四五六七八九十")
        self.config.reading.daily_max=1
        await self.service.tick(NOW,STUDY)
        self.assertEqual(await self.make_service().tick(NOW+timedelta(hours=1),STUDY),{"status":"daily_limit"})
        self.config.reading.daily_max=4
        self.assertEqual(await self.make_service().tick(NOW+timedelta(minutes=59),STUDY),{"status":"interval"})
        result=await self.make_service().tick(NOW+timedelta(days=1),STUDY)
        self.assertEqual(result["status"],"read")
        usage=await self.store.reading_usage(NOW.timestamp(),(NOW+timedelta(days=1)).timestamp())
        self.assertEqual(usage,{"count":1,"last_read_at":(NOW+timedelta(days=1)).timestamp()})

    async def test_disabled_busy_and_zero_quota_never_open_source(self):
        with patch.object(self.service,"_source_directory",side_effect=AssertionError("不应读取书源")):
            self.config.reading.enabled=False
            self.assertEqual(await self.service.tick(NOW,STUDY),{"status":"disabled"})
            self.config.reading.enabled=True
            self.assertEqual(await self.service.tick(NOW,{"current":{"kind":"rest"}}),{"status":"schedule_busy"})
            self.config.reading.daily_max=0
            self.assertEqual(await self.service.tick(NOW,STUDY),{"status":"daily_limit"})

    async def test_no_source_and_failure_are_rate_limited(self):
        self.assertEqual(await self.service.tick(NOW,STUDY),{"status":"no_source"})
        self.book()
        self.assertEqual(await self.service.tick(NOW+timedelta(minutes=1),STUDY),{"status":"interval"})
        self.assertEqual((await self.service.tick(NOW+timedelta(hours=1),STUDY))["status"],"read")

    async def test_rejects_bad_files_and_reads_only_bounded_utf8_text(self):
        (self.novels/"a-invalid.txt").write_bytes(b"\xff\xfe\xff")
        (self.novels/"b-binary.txt").write_bytes(b"valid\x00binary")
        self.book("c-empty.txt","   ")
        with (self.novels/"d-large.txt").open("wb") as stream:stream.truncate(reading._MAX_FILE_BYTES+1)
        self.book("e-novel.md","不会读取")
        (self.novels/"f-utf8.txt").write_bytes(b"\xef\xbb\xbf"+"真实文本".encode("utf-8"))
        self.assertEqual((await self.service.tick(NOW,STUDY))["status"],"read")
        self.assertEqual((await self.store.recent_local_reading_notes())[0]["excerpt"],"真实文本")

    async def test_rejects_traversal_and_absolute_source_directory(self):
        for directory in ("../outside",str(self.root/"novels")):
            with self.subTest(directory=directory):
                self.config.reading.directory=directory
                self.assertEqual(await self.make_service().tick(NOW,STUDY),{"status":"no_source"})
        self.assertEqual(await self.store.recent_local_reading_notes(),[])

    async def test_rejects_linked_file_and_linked_directory(self):
        outside=self.root/"outside.txt"; outside.write_text("不读取链接",encoding="utf-8")
        try:(self.novels/"a-link.txt").symlink_to(outside)
        except OSError:self.skipTest("系统未开放符号链接创建权限")
        self.assertEqual(await self.service.tick(NOW,STUDY),{"status":"no_source"})
        linked=self.root/"linked"; linked.symlink_to(self.novels,target_is_directory=True)
        self.config.reading.directory="linked"
        self.assertEqual(await self.make_service().tick(NOW,STUDY),{"status":"no_source"})

    async def test_model_sees_actual_range_and_failures_keep_excerpt(self):
        self.book()
        self.llm.task_available.return_value=True; self.llm.generate.return_value="人物观察：片段只有第一章。"
        self.service.set_bot_name("小麦")
        await self.service.tick(NOW,STUDY)
        prompt=self.llm.generate.await_args.args[0]
        self.assertIn("小麦",prompt); self.assertIn('"text": "第一章。"',prompt)
        self.assertIn('"range": "1-4/12"',prompt)
        self.assertIn("不能声称读过未提供的章节",prompt)
        self.assertEqual((await self.store.recent_local_reading_notes())[0]["annotation"],"人物观察：片段只有第一章。")
        self.llm.generate.side_effect=RuntimeError("offline")
        await self.service.tick(NOW+timedelta(hours=1),STUDY)
        latest=(await self.store.recent_local_reading_notes())[0]
        self.assertIn("未生成批注",latest["annotation"])
        self.assertEqual(latest["excerpt"],"第二章。")

    async def test_context_only_reaches_owner_and_configuration_can_update(self):
        self.book(); await self.service.tick(NOW,STUDY)
        with patch.object(self.store,"recent_local_reading_notes",side_effect=AssertionError("朋友不能查询正文")):
            self.assertEqual(await self.service.context_for_user({"role":"friend"}),{"items":[]})
            self.assertEqual(await self.service.context_for_user({}),{"items":[]})
        context=await self.service.context_for_user({"role":"owner"})
        self.assertEqual(context["items"][0]["source"],"novels/私人小说.txt")
        self.assertEqual(context["items"][0]["range"],"1-4/12")
        self.assertEqual(context["items"][0]["excerpt"],"第一章。")
        self.service.set_bot_name(""); self.assertEqual(self.service.bot_name,"麦麦")
        config=SimpleNamespace(reading=SimpleNamespace(enabled=False))
        self.service.update_config(config)
        self.assertEqual(await self.service.tick(NOW,STUDY),{"status":"disabled"})

    async def test_note_and_progress_rollback_together_on_database_error(self):
        self.book()
        self.store.conn.execute("""CREATE TRIGGER reject_progress BEFORE INSERT ON local_reading_progress
                                 BEGIN SELECT RAISE(ABORT,'reject progress'); END""")
        result=await self.service.tick(NOW,STUDY)
        self.assertEqual(result,{"status":"failed"})
        for table in ("local_reading_progress","local_reading_chunks","reading_notes","bookshelf_documents"):
            self.assertEqual(self.store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],0)
        self.store.conn.execute("DROP TRIGGER reject_progress")
        self.assertEqual((await self.service.tick(NOW+timedelta(hours=1),STUDY))["status"],"read")
        self.assertEqual((await self.store.recent_local_reading_notes())[0]["excerpt"],"第一章。")

    async def test_concurrent_readers_save_one_segment(self):
        self.book(); self.llm.task_available.return_value=True
        arrived=asyncio.Event(); calls=0
        async def annotation(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==2:arrived.set()
            await asyncio.wait_for(arrived.wait(),1)
            return "只阅读实际片段"
        self.llm.generate.side_effect=annotation
        results=await asyncio.gather(self.service.tick(NOW,STUDY),self.make_service().tick(NOW,STUDY))
        self.assertEqual(sorted(result["status"] for result in results),["duplicate","read"])
        self.assertEqual(len(await self.store.recent_local_reading_notes()),1)

    async def test_storage_rejects_inconsistent_range_before_writing(self):
        with self.assertRaisesRegex(ValueError,"范围"):
            await self.store.save_reading_chunk({},source_path="novels/a.txt",start_offset=0,end_offset=4,
                total_chars=4,excerpt="短",day_start=0,day_end=NOW.timestamp()+1,daily_max=1,interval_seconds=60)
        self.assertEqual(await self.store.reading_progress("missing"),0)
        with self.assertRaisesRegex(ValueError,"来源"):
            await self.store.save_reading_chunk({"source_api":"external"},source_path="novels/a.txt",start_offset=0,
                end_offset=1,total_chars=1,excerpt="字",day_start=0,day_end=NOW.timestamp()+1,daily_max=1,interval_seconds=60)

    async def test_external_reading_note_api_keeps_existing_behavior(self):
        item={"id":"external-1","source_api":"other.reading","external_id":"chapter-1","title":"外部文字",
              "summary":"真实摘要","annotation":"私人批注","source_digest":"digest","created_at":NOW.timestamp()}
        self.assertTrue(await self.store.save_reading_note(item))
        self.assertFalse(await self.store.save_reading_note(item))
        self.assertEqual((await self.store.get_bookshelf_document("reading:external-1",allow_private=True))["content"],"私人批注")


if __name__=="__main__":unittest.main()
