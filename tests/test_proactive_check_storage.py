"""主动判断的持久冷却、边界和跨连接并发去重。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib
import sqlite3
from threading import Barrier

import pytest


@pytest.fixture
def store(plugin_module,tmp_path):
    storage=importlib.import_module("mai_integration_tests.core.storage")
    database=storage.LifeStore(str(tmp_path),journal_mode="delete")
    asyncio.run(database.initialize())
    yield database
    asyncio.run(database.close())


@pytest.mark.parametrize("status",["pending","expired","failed","cancelled","sent"])
def test_every_decision_status_counts_and_exact_interval_boundary_passes(store,status):
    async def check():
        assert await store.add_proactive_pending("old","u","op","stream",100,120)
        store.conn.execute("UPDATE proactive_events SET status=? WHERE id='old'",(status,))
        store.conn.commit()
        assert await store.last_proactive_checks(["u"])=={"u":100}
        assert not await store.add_proactive_pending("early","u","op","stream",159.999,200,min_interval_seconds=60)
        assert await store.add_proactive_pending("due","u","op","stream",160,200,min_interval_seconds=60)
        assert await store.last_proactive_checks(["u"])=={"u":160}
    asyncio.run(check())


def test_pending_blocks_until_its_exact_expiry_even_if_check_interval_elapsed(store):
    async def check():
        await store.add_proactive_pending("old","u","op","stream",100,200)
        assert not await store.add_proactive_pending("early","u","op","stream",199.999,300,min_interval_seconds=60)
        assert await store.add_proactive_pending("due","u","op","stream",200,300,min_interval_seconds=60)
    asyncio.run(check())


def test_batch_checks_accept_empty_iterables_and_are_independent_per_user(store):
    async def check():
        await store.add_proactive_pending("a","u","op","stream",100,200)
        await store.add_proactive_pending("b","u","op","stream",150,200)
        assert await store.add_proactive_pending("c","v","op","stream-v",151,200,min_interval_seconds=60)
        quoted="u' OR 1=1 --"
        await store.add_proactive_pending("d",quoted,"op","stream-x",120,200)
        assert await store.last_proactive_checks([])=={}
        assert await store.last_proactive_checks(iter(["u","v","missing","u","",quoted]))=={"u":150,"v":151,quoted:120}
        assert await store.last_proactive_checks(["missing"])=={}
    asyncio.run(check())


def test_restart_keeps_check_time_even_when_planner_did_not_reply(store):
    async def check():
        await store.add_proactive_pending("old","u","op","stream",100,110)
        await store.expire_pending(120)
        await store.close()
        await store.initialize()
        assert await store.last_proactive_checks(["u"])=={"u":100}
        assert not await store.add_proactive_pending("early","u","op","stream",130,200,min_interval_seconds=60)
        assert await store.add_proactive_pending("due","u","op","stream",160,200,min_interval_seconds=60)
    asyncio.run(check())


def test_concurrent_checks_for_one_user_only_reserve_once(store):
    async def check():
        results=await asyncio.gather(*(store.add_proactive_pending(str(index),"u","op","stream",100,200,
            min_interval_seconds=60) for index in range(3)))
        assert results.count(True)==1
        assert store.conn.execute("SELECT COUNT(*) FROM proactive_events").fetchone()[0]==1
    asyncio.run(check())


def test_cross_connection_checks_are_atomic(store):
    other=type(store)(str(store.data_dir),journal_mode="delete")
    asyncio.run(other.initialize())
    barrier=Barrier(2)

    def reserve(database,event_id):
        barrier.wait(timeout=5)
        return asyncio.run(database.add_proactive_pending(event_id,"u","op","stream",100,200,min_interval_seconds=60))

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts=[pool.submit(reserve,database,event_id) for database,event_id in ((store,"a"),(other,"b"))]
            assert sorted(attempt.result(timeout=10) for attempt in attempts)==[False,True]
        assert store.conn.execute("SELECT COUNT(*) FROM proactive_events").fetchone()[0]==1
    finally:asyncio.run(other.close())


def test_legacy_calls_keep_allowing_multiple_pending_and_failed_insert_rolls_back(store):
    async def check():
        assert await store.add_proactive_pending("a","u","op","stream",100,200)
        assert await store.add_proactive_pending("b","u","op","stream",101,200)
        with pytest.raises(sqlite3.IntegrityError):
            await store.add_proactive_pending("a","v","op","stream-v",100,200,min_interval_seconds=60)
        assert await store.add_proactive_pending("c","v","op","stream-v",100,200,min_interval_seconds=60)
    asyncio.run(check())
