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
