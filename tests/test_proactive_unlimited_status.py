"""主动无限次数配置在公共接口和管理摘要中的展示。"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


NOW=datetime(2026,11,5,12,tzinfo=timezone.utc)


@pytest.fixture
def user():
    return {"user_id":"12345","enabled":1,"proactive_enabled":1,"role":"friend",
            "temperature":30,"daily_proactive_max":5,"proactive_count":3}


@pytest.mark.parametrize("unlimited",[False,True,None])
def test_relationship_and_webui_expose_unlimited_flag(plugin_module,user,unlimited):
    if unlimited is not None:user["daily_proactive_unlimited"]=int(unlimited)
    plugin=plugin_module.MaiLifePlugin()
    plugin._store=SimpleNamespace(get_user=AsyncMock(return_value=user),
        list_users=AsyncMock(return_value=[user]),recent_interactions=AsyncMock(return_value=[]))
    plugin._env=SimpleNamespace(now=Mock(return_value=NOW))
    relationship=asyncio.run(plugin.api_get_relationship("12345"))
    webui=asyncio.run(plugin.webui_users())
    for result in (relationship,webui["users"][0]):
        assert result["daily_proactive_unlimited"] is bool(unlimited)
        assert result["daily_proactive_max"]==5
    assert "stream_id" not in relationship


@pytest.mark.parametrize("unlimited,expected",[(False,"3/5"),(True,"3/无上限"),(None,"3/5")])
def test_management_summary_displays_effective_limit(plugin_module,user,unlimited,expected):
    if unlimited is not None:user["daily_proactive_unlimited"]=int(unlimited)
    admin=plugin_module.AdminService(SimpleNamespace(list_users=AsyncMock(return_value=[user])),None)
    snapshot=asyncio.run(admin.snapshot("users",NOW))
    assert snapshot["items"][0]["daily_proactive_unlimited"] is bool(unlimited)
    assert f"主动 {expected}" in asyncio.run(admin.format_text("users",NOW))
