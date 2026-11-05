"""移除创作后保留私人日记、阅读笔记和旧库历史。"""
import importlib.util
from pathlib import Path
import tempfile
import unittest


def _load(name,path):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).parents[1]/path)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


storage=_load("mai_retained_storage","core/storage.py")
bookshelf=_load("mai_retained_bookshelf","creation/bookshelf_service.py")
NOW=1793851200.0
DAY="2026-11-05"


class CreationRemovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.store=storage.LifeStore(self.directory.name,clock=lambda:NOW)
        await self.store.initialize()
        self.shelf=bookshelf.BookshelfService(self.store,None)
        for privacy in ("public","private"):
            self.store.conn.execute(
                """INSERT INTO bookshelf_documents
                (id,doc_type,work_type,title,privacy,status,current_revision,created_at,updated_at)
                VALUES(?,'work','novel_fragment','旧作品',?,'archived',1,?,?)""",
                (f"work-{privacy}",privacy,NOW,NOW),
            )
            self.store.conn.execute(
                """INSERT INTO bookshelf_revisions
                (document_id,revision_number,stage,content,created_at) VALUES(?,1,'final','历史正文',?)""",
                (f"work-{privacy}",NOW),
            )
        self.store.conn.commit()
        await self.store.save_diary(DAY,"新闻日记","整理了新闻。","平静","diary-digest",NOW)
        await self.store.save_reading_note({"id":"note","source_api":"local_novel","external_id":"chapter",
            "title":"小说阅读","summary":"读过的片段","annotation":"私人批注","source_digest":"digest","created_at":NOW})

    async def asyncTearDown(self):
        await self.store.close()
        self.directory.cleanup()

    async def test_diaries_and_reading_remain_private_and_old_works_are_hidden(self):
        owner={"role":"owner"}; friend={"role":"friend"}
        expected={f"diary:{DAY}","reading:note"}
        self.assertEqual({item["id"] for item in await self.shelf.list_for_user(owner)},expected)
        self.assertEqual(await self.shelf.list_for_user(friend),[])
        self.assertEqual({item["type"] for item in (await self.shelf.context_for_user(owner))["items"]},{"日记","阅读笔记"})
        for document_id in expected:
            self.assertTrue((await self.shelf.read_for_user(document_id,owner))["content"])
            self.assertEqual(await self.shelf.read_for_user(document_id,friend),{})
            self.assertTrue(await self.shelf.read_for_user(document_id,friend,is_admin=True))
        for privacy in ("public","private"):
            self.assertEqual(await self.shelf.read_for_user(f"work-{privacy}",owner,is_admin=True),{})
        self.assertEqual(await self.shelf.read_for_user("missing",owner),{})
        self.assertEqual(await self.shelf.list_for_user(owner,doc_type="work"),[])
        self.assertEqual({item["id"] for item in await self.store.management_bookshelf()},expected)
        counts=await self.store.management_overview_counts()
        self.assertEqual((counts["bookshelf_documents"],counts["private_documents"]),(2,2))

    async def test_upgrade_and_cleanup_preserve_old_records_without_reactivating_creation(self):
        tables={row[0] for row in self.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse(tables.intersection({"creation_inspirations","creation_runs"}))
        for table in ("creation_inspirations","creation_runs"):
            self.store.conn.execute(f"CREATE TABLE {table}(payload TEXT NOT NULL)")
            self.store.conn.execute(f"INSERT INTO {table} VALUES('旧创作记录')")
        self.store.conn.execute("UPDATE meta SET value='17' WHERE key='schema_version'")
        self.store.conn.commit()
        await self.store.close()
        await self.store.initialize()
        await self.store.cleanup_runtime_records(NOW,NOW,records_before=NOW-90*86400,day=DAY)
        for table in ("creation_inspirations","creation_runs"):
            self.assertEqual(self.store.conn.execute(f"SELECT payload FROM {table}").fetchone()[0],"旧创作记录")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM bookshelf_documents WHERE doc_type='work'").fetchone()[0],2)
        self.assertEqual(self.store.conn.execute("SELECT content FROM bookshelf_revisions WHERE document_id='work-public'").fetchone()[0],"历史正文")
        self.assertEqual((await self.store.get_diary(DAY))["content"],"整理了新闻。")
        self.assertEqual((await self.shelf.read_for_user("reading:note",{"role":"owner"}))["content"],"私人批注")
        self.assertFalse(list(Path(self.directory.name).glob("*.incompatible.*")))

    async def test_previous_creation_opportunities_cannot_be_selected(self):
        for key,framework,privacy in (("private-work","creation:work-1","owner_only"),
                ("public-work","creation:work-2","public_work"),("old-public-work","old:work","public_work"),
                ("diary",f"diary:{DAY}","owner_only"),("news","news:1","external")):
            await self.store.add_opportunity({"id":key,"framework_id":framework,"privacy":privacy,
                "topic":"保留记录","motive":"分享","weight":0.5,"expires_at":NOW+3600})
        self.assertEqual({item["id"] for item in await self.store.active_opportunities(NOW)},{"diary","news"})
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM proactive_opportunities").fetchone()[0],5)


if __name__=="__main__":unittest.main()
