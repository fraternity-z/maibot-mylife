"""主动次数不限的配置兼容、持久化与增量升级。"""
import asyncio
import importlib
import sqlite3

import pytest
from pydantic import ValidationError


@pytest.fixture
def modules(plugin_module):
    return (importlib.import_module("mai_integration_tests.config"),
            importlib.import_module("mai_integration_tests.core.storage"))


@pytest.mark.parametrize("fields,expected",[
    ({},False),({"proactive_enabled":True},True),({"proactive_enabled":False},False),
])
def test_proactive_opt_in_survives_reload_and_filters_candidates(modules,tmp_path,fields,expected):
    config,storage=modules
    profile=config.UserProfile(user_id="123",enabled=True,**fields)
    reloaded=config.UserProfile.model_validate(profile.model_dump())
    assert reloaded.proactive_enabled is expected

    async def check():
        store=storage.LifeStore(str(tmp_path))
        try:
            await store.initialize()
            await store.sync_users([reloaded])
            await store.close()
            await store.initialize()
            assert bool((await store.get_user("123"))["proactive_enabled"]) is expected
            assert [user["user_id"] for user in await store.list_users(proactive_only=True)]==(
                ["123"] if expected else [])
        finally:await store.close()

    asyncio.run(check())


@pytest.mark.parametrize("role,limit,expected",[("friend",-1,1),("owner",-1,2),("friend",0,0),("owner",20,20)])
def test_existing_limits_keep_their_meaning(modules,role,limit,expected):
    config,_=modules
    profile=config.UserProfile(user_id="123",role=role,daily_proactive_max=limit)
    assert profile.daily_proactive_max==expected
    assert profile.daily_proactive_unlimited is False
    unlimited=config.UserProfile(**{**profile.model_dump(),"daily_proactive_unlimited":True})
    assert unlimited.daily_proactive_unlimited is True
    assert unlimited.daily_proactive_max==expected


@pytest.mark.parametrize("fields",[{"daily_proactive_unlimited":"sometimes"},{"daily_proactive_max":21}])
def test_invalid_proactive_config_is_rejected(modules,fields):
    config,_=modules
    with pytest.raises(ValidationError):config.UserProfile(user_id="123",**fields)


def test_sync_can_toggle_unlimited_without_resetting_relationship_or_count(modules,tmp_path):
    config,storage=modules

    async def check():
        store=storage.LifeStore(str(tmp_path))
        try:
            await store.initialize()
            profile=config.UserProfile(user_id="123",daily_proactive_max=0)
            await store.sync_users([profile])
            store.conn.execute("UPDATE users SET temperature=75,proactive_count=9,proactive_day='2026-11-05',"
                               "last_proactive_at=100,last_user_message_at=90,stream_id='private' WHERE user_id='123'")
            store.conn.commit()
            for unlimited in (False,True,False):
                profile.daily_proactive_unlimited=unlimited
                await store.sync_users([profile])
                user=await store.get_user("123")
                assert bool(user["daily_proactive_unlimited"]) is unlimited
                assert user["daily_proactive_max"]==0
                assert (user["temperature"],user["proactive_count"],user["proactive_day"],
                        user["last_proactive_at"],user["last_user_message_at"],user["stream_id"])==(75,9,"2026-11-05",100,90,"private")
            profile.daily_proactive_unlimited=True
            await store.sync_users([profile])
            await store.close()
            await store.initialize()
            assert (await store.get_user("123"))["daily_proactive_unlimited"]==1
        finally:await store.close()

    asyncio.run(check())


def test_existing_database_adds_unlimited_column_without_losing_users(modules,tmp_path):
    _,storage=modules
    with sqlite3.connect(tmp_path/"mai_life.db") as conn:
        conn.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO meta VALUES('schema_version','15');
            CREATE TABLE users(
              user_id TEXT PRIMARY KEY,enabled INTEGER NOT NULL,proactive_enabled INTEGER NOT NULL,
              display_name TEXT NOT NULL,temperature REAL NOT NULL,
              role TEXT NOT NULL DEFAULT 'friend',daily_proactive_max INTEGER NOT NULL DEFAULT 1,
              quiet_start TEXT NOT NULL,quiet_end TEXT NOT NULL,
              stream_id TEXT NOT NULL DEFAULT '',last_user_message_at REAL NOT NULL DEFAULT 0,
              last_proactive_at REAL NOT NULL DEFAULT 0,
              proactive_day TEXT NOT NULL DEFAULT '',proactive_count INTEGER NOT NULL DEFAULT 0,
              last_relation_day TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO users(user_id,enabled,proactive_enabled,display_name,temperature,quiet_start,quiet_end,
                              proactive_count,proactive_day,daily_proactive_max)
            VALUES('123',1,1,'旧昵称',65,'00:00','08:00',8,'2026-11-05',0);
        """)

    async def check():
        store=storage.LifeStore(str(tmp_path))
        try:
            await store.initialize()
            user=await store.get_user("123")
            assert user["daily_proactive_unlimited"]==0
            assert (user["display_name"],user["temperature"],user["proactive_count"],user["daily_proactive_max"])==("旧昵称",65,8,0)
            await store.initialize()
            assert await store.get_user("123")==user
            assert int(store.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])==storage.SCHEMA_VERSION
            assert not list(tmp_path.glob("*.incompatible.*"))
        finally:await store.close()

    asyncio.run(check())
