"""页面删减后仍能解析，固定 DOM 引用完整且不再读取生理模拟数据。"""
from pathlib import Path
import json
import re
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("page",["overview","life","system"])
def test_daily_life_page_has_valid_dom_references_and_script(page):
    source=(Path(__file__).parents[1]/"webui"/f"{page}.html").read_text(encoding="utf-8")
    ids=set(re.findall(r'''\bid=["']([^"']+)["']''',source))
    references=set(re.findall(r'''getElementById\(["']([^"']+)["']\)''',source))
    assert references<=ids, f"{page} 引用了已删除的 DOM: {references-ids}"
    assert not re.search(r"energy|hunger|mood_valence|mood_arousal|sleep_phase|health_note|dream|recall",source,re.I)
    assert "applied" not in source
    assert not re.search(r'''name:\s*["']history["']''',source)
    pages=json.loads((Path(__file__).parents[1]/"webui.json").read_text(encoding="utf-8"))["pages"]
    queries=next(item["queries"] for item in pages if item["id"]==page)
    requests=set(re.findall(r'''name:\s*["']([^"']+)["']''',source))
    assert requests<=queries.keys()
    assert all(query["api"]!="webui_status_history" for query in queries.values())
    scripts=re.findall(r"<script\b[^>]*>(.*?)</script>",source,re.S|re.I)
    assert scripts
    node=shutil.which("node")
    if node is None:pytest.skip("需要 Node.js 检查页面脚本语法")
    for script in scripts:
        result=subprocess.run([node,"--check"],input=script,encoding="utf-8",capture_output=True,timeout=15)
        assert result.returncode==0, result.stderr


@pytest.mark.parametrize("unlimited,label,dots",[(False,"3/5",5),(True,"3/无上限",0)])
def test_user_cards_display_unlimited_quota_without_finite_dots(unlimited,label,dots):
    node=shutil.which("node")
    if node is None:pytest.skip("需要 Node.js 检查用户卡片渲染")
    source=(Path(__file__).parents[1]/"webui"/"life.html").read_text(encoding="utf-8")
    render="function renderUsers() {"+source.split("function renderUsers() {",1)[1].split(
        "// ==================== 2. Render Dates",1)[0]
    user={"user_id":"12345","enabled":True,"proactive_enabled":True,
          "daily_proactive_unlimited":unlimited,"daily_proactive_max":5,"proactive_count":3}
    state={"users":{"users":[user]},"userSearch":"","userFilter":"all"}
    script=("const state="+json.dumps(state)+";const nodes={};const esc=String;"
            "const document={getElementById(id){return nodes[id]??={};}};"+render+
            "renderUsers();console.log(nodes['users-cards-grid'].innerHTML);")
    result=subprocess.run([node],input=script,encoding="utf-8",capture_output=True,timeout=15)
    assert result.returncode==0, result.stderr
    assert label in result.stdout
    assert result.stdout.count('class="ml-quota-dot ')==dots
