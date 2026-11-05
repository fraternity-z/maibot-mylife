"""主动判断与发送间隔合并后，配置和诊断只显示统一设置。"""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

import pytest


NOW=datetime(2026,11,5,12,tzinfo=timezone.utc)


@pytest.mark.parametrize("interval",[7,180])
def test_config_summary_keeps_only_unified_interval(plugin_module,interval):
    plugin=plugin_module.MaiLifePlugin()
    plugin.config=plugin_module.MaiLifeSettings()
    plugin.config.proactive.patrol_interval_minutes=interval
    with patch.object(plugin_module.MaiLifePlugin,"_ready",new_callable=PropertyMock,return_value=True):
        result=asyncio.run(plugin.webui_config_summary())
    assert result["proactive"]["patrol_interval_minutes"]==interval
    assert "creation" not in result and "creation" not in result["editable"]
    assert result["network"]["news_sources"]==["ithome","solidot","sspai"]
    assert "news_sources" not in result["editable"]["network"]
    assert result["editable"]["proactive"]["patrol_interval_minutes"]=="int"
    assert "min_interval_minutes" not in result["proactive"]
    assert "min_interval_minutes" not in result["editable"]["proactive"]


@pytest.mark.parametrize("reason,label",[("planner_interval","Planner 判断间隔未到"),("low_score","生活参考未达阈值")])
def test_planner_skip_reaches_api_and_management(plugin_module,reason,label):
    skips=[{"reason":reason,"total":2,"users":1}]
    store=SimpleNamespace(management_proactive_candidates=AsyncMock(return_value=[]),
        proactive_skip_summary=AsyncMock(return_value=skips))
    admin=plugin_module.AdminService(store,None)
    assert f"{label}：2 次" in asyncio.run(admin.format_text("proactive",NOW))
    plugin=plugin_module.MaiLifePlugin()
    plugin._store=store
    plugin._env=SimpleNamespace(now=Mock(return_value=NOW))
    plugin._admin=SimpleNamespace(snapshot=AsyncMock(return_value={}))
    result=asyncio.run(plugin.webui_admin_summary())
    assert result["proactive"]["skips"]==skips


def test_proactive_loop_checks_each_minute_and_preserves_cancellation(plugin_module,monkeypatch):
    plugin=plugin_module.MaiLifePlugin()
    plugin._store=Mock()
    plugin._env=SimpleNamespace(now=Mock(return_value=NOW))
    plugin._proactive=SimpleNamespace(patrol=AsyncMock())
    plugin._resolve_all_streams=AsyncMock()
    sleep=AsyncMock(side_effect=[None,asyncio.CancelledError])
    monkeypatch.setattr(plugin_module.asyncio,"sleep",sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(plugin._proactive_loop())
    assert [item.args for item in sleep.await_args_list]==[(60,),(60,)]
    plugin._resolve_all_streams.assert_awaited_once_with()
    plugin._proactive.patrol.assert_awaited_once_with(NOW)


@pytest.mark.parametrize("search",["","主动判断与发送"])
def test_system_displays_and_searches_section_specific_interval(search):
    node=shutil.which("node")
    if node is None:pytest.skip("需要 Node.js 检查诊断页渲染")
    source=(Path(__file__).parents[1]/"webui"/"system.html").read_text(encoding="utf-8")
    labels="const SECTION_META ="+source.split("const SECTION_META =",1)[1].split("function esc",1)[0]
    render="function renderConfig() {"+source.split("function renderConfig() {",1)[1].split(
        "// ---- 配置回写编辑器",1)[0]
    state={"config":{"proactive":{"patrol_interval_minutes":7},
                     "network":{"news_sources":["ithome","solidot","sspai"]}},"cfgSearch":search}
    script=("const state="+json.dumps(state)+";const nodes={};const esc=String;"
        "const document={getElementById(id){return nodes[id]??={};}};"+labels+render+
        "renderConfig();console.log(nodes['cfg-cards-grid'].innerHTML);")
    result=subprocess.run([node],input=script,encoding="utf-8",capture_output=True,timeout=15)
    assert result.returncode==0, result.stderr
    assert "主动判断与发送间隔（分）" in result.stdout
    assert ('内置新闻源' in result.stdout) is (not search)
    assert ('ithome,solidot,sspai' in result.stdout) is (not search)
    assert 'data-key="news_sources"' not in result.stdout


@pytest.mark.parametrize("reason,label",[("planner_interval","Planner 判断间隔未到"),("low_score","生活参考未达阈值")])
def test_system_displays_planner_skip_label(reason,label):
    node=shutil.which("node")
    if node is None:pytest.skip("需要 Node.js 检查诊断页渲染")
    source=(Path(__file__).parents[1]/"webui"/"system.html").read_text(encoding="utf-8")
    render="function renderAdmin() {"+source.split("function renderAdmin() {",1)[1].split(
        "function renderConfig()",1)[0]
    state={"admin":{"proactive":{"skips":[{"reason":reason,"total":2,"users":1}]}}}
    script=("const state="+json.dumps(state)+";const nodes={};const esc=String;"
        "const document={getElementById(id){return nodes[id]??={};}};"+render+
        "renderAdmin();console.log(nodes['proactive-skips-bars'].innerHTML);")
    result=subprocess.run([node],input=script,encoding="utf-8",capture_output=True,timeout=15)
    assert result.returncode==0, result.stderr
    assert label in result.stdout
    assert "跳过 <strong>2</strong> 次" in result.stdout
