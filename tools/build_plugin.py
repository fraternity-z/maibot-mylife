"""从当前工作树按白名单生成可安装 ZIP，仅使用标准库。"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile


PACKAGE_FILES = """
_manifest.json __init__.py plugin.py config.py requirements.txt webui.json mailife.png LICENSE
README.md CHANGELOG.md config.toml.example docs/schedule-provider-api.md
core/__init__.py core/environment.py core/llm_service.py core/storage.py core/network_clock.py
creation/__init__.py creation/bookshelf_service.py creation/creation_service.py creation/inspiration_service.py
information/__init__.py information/host_search.py information/http_client.py
information/information_service.py information/news_service.py
life/__init__.py life/bedtime.py life/continuity.py life/life_state.py life/memory_service.py
life/proactive.py life/rest_gate.py life/schedule_service.py
management/__init__.py management/admin_service.py
messaging/__init__.py messaging/adapter_compat.py messaging/command_catalog.py messaging/command_reply.py
messaging/message_pipeline.py messaging/prompt_builder.py messaging/recall_service.py messaging/task_context.py
social/__init__.py social/group_observer.py social/relay_service.py
webui/overview.html webui/life.html webui/system.html
""".split()


def _source(root: Path, relative: str) -> Path:
    if (not isinstance(relative,str) or not relative or "\\" in relative or ":" in relative
            or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts):
        raise ValueError(f"包资源路径必须在插件目录内：{relative!r}")
    path=root
    for part in PurePosixPath(relative).parts:
        path=path/part
        if path.is_symlink() or getattr(path,"is_junction",lambda:False)():
            raise ValueError(f"包资源不能是链接：{relative}")
    if not path.is_file():raise FileNotFoundError(f"缺少打包文件：{relative}")
    path.resolve().relative_to(root)
    return path


def build_package(root: Path, output_dir: Path) -> Path:
    if root.is_symlink() or getattr(root,"is_junction",lambda:False)():
        raise ValueError("插件根目录不能是链接")
    root=root.resolve()
    payloads={name:_source(root,name).read_bytes() for name in PACKAGE_FILES}
    manifest=json.loads(payloads["_manifest.json"])
    if not isinstance(manifest,dict):raise ValueError("_manifest.json 必须是 JSON 对象")
    version=manifest.get("version")
    if not isinstance(version,str) or not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)",version,re.ASCII):
        raise ValueError("manifest.version 必须是 x.y.z 格式的版本号")
    config=ast.parse(payloads["config.py"],filename="config.py")
    versions=[ast.literal_eval(node.value) for node in config.body if isinstance(node,ast.Assign)
              and any(isinstance(target,ast.Name) and target.id=="PLUGIN_VERSION" for target in node.targets)]
    if versions!=[version]:raise ValueError("manifest.version 与 config.PLUGIN_VERSION 不一致")
    icon=manifest["display"]["icon"]
    resources=[icon["value"]] if icon["type"]=="local" else []
    resources.extend(page["entry"] for page in json.loads(payloads["webui.json"])["pages"])
    for name in resources:
        _source(root,name)
        if name not in payloads:raise ValueError(f"资源未列入打包白名单：{name}")
    for name,data in payloads.items():
        if name.endswith(".py"):compile(data,name,"exec")
    output_dir.mkdir(parents=True,exist_ok=True)
    target=output_dir/f"mai-mylife-{version}.zip"
    descriptor,temporary_name=tempfile.mkstemp(prefix=target.stem+"-",suffix=".tmp",dir=output_dir)
    os.close(descriptor)
    temporary=Path(temporary_name)
    try:
        with ZipFile(temporary,"w",compression=ZIP_DEFLATED) as archive:
            for name,data in sorted(payloads.items()):archive.writestr(name,data)
        os.replace(temporary,target)
    finally:
        temporary.unlink(missing_ok=True)
    return target.resolve()


def main() -> int:
    parser=argparse.ArgumentParser(description="打包麦日常插件（仅包含发布白名单文件）")
    parser.add_argument("--output-dir",type=Path,help="输出目录，默认插件目录下 dist")
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    try:result=build_package(root,args.output_dir or root/"dist")
    except (OSError,ValueError,SyntaxError,KeyError,TypeError) as exc:parser.exit(1,f"打包失败：{exc}\n")
    print(result)
    return 0


if __name__=="__main__":raise SystemExit(main())
