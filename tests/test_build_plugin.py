"""发布包只包含工作树白名单，校验或写入失败不破坏旧包。"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile


_SPEC=importlib.util.spec_from_file_location("mai_build_plugin",Path(__file__).parents[1]/"tools"/"build_plugin.py")
builder=importlib.util.module_from_spec(_SPEC); _SPEC.loader.exec_module(builder)


class BuildPluginTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)/"plugin"; self.output=Path(self.temporary.name)/"dist"
        for name in builder.PACKAGE_FILES:
            path=self.root/name; path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text("# fixture\n",encoding="utf-8")
        self.manifest={"version":"1.2.3","display":{"icon":{"type":"local","value":"mailife.png"}}}
        self.write_manifest()
        (self.root/"config.py").write_text('PLUGIN_VERSION = "1.2.3"\n',encoding="utf-8")
        self.webui={"pages":[{"entry":f"webui/{name}.html"} for name in ("overview","life","system")]}
        (self.root/"webui.json").write_text(json.dumps(self.webui),encoding="utf-8")

    def write_manifest(self):
        (self.root/"_manifest.json").write_text(json.dumps(self.manifest),encoding="utf-8")

    def old_package(self):
        self.output.mkdir(exist_ok=True)
        path=self.output/"mai-mylife-1.2.3.zip"; path.write_bytes(b"previous release")
        return path

    def test_packages_current_worktree_and_excludes_private_files(self):
        (self.root/"core/network_clock.py").write_text('MARKER = "untracked working tree"\n',encoding="utf-8")
        for name in ("config.toml","data/private.db",".git/config",".codegraph/index.json",
                     "core/private.py","core/__pycache__/secret.pyc","tests/private.txt","debug.log"):
            path=self.root/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_text("secret",encoding="utf-8")
        result=builder.build_package(self.root,self.output)
        self.assertEqual(result.name,"mai-mylife-1.2.3.zip")
        with ZipFile(result) as archive:
            self.assertEqual(set(archive.namelist()),set(builder.PACKAGE_FILES))
            self.assertEqual(len(archive.namelist()),47)
            self.assertTrue({"life/bedtime.py","life/life_state.py","messaging/recall_service.py"}.isdisjoint(archive.namelist()))
            self.assertIn(b"untracked working tree",archive.read("core/network_clock.py"))
            self.assertIn("_manifest.json",archive.namelist())
        self.assertEqual(list(self.output.glob("*.tmp")),[])

    def test_missing_required_file_preserves_old_package(self):
        old=self.old_package(); (self.root/"core/network_clock.py").unlink()
        with self.assertRaisesRegex(FileNotFoundError,"core/network_clock.py"):
            builder.build_package(self.root,self.output)
        self.assertEqual(old.read_bytes(),b"previous release")

    def test_version_mismatch_is_rejected(self):
        (self.root/"config.py").write_text('PLUGIN_VERSION = "1.2.4"\n',encoding="utf-8")
        with self.assertRaisesRegex(ValueError,"不一致"):builder.build_package(self.root,self.output)

    def test_bad_versions_cannot_become_output_paths(self):
        for version in ("../1.2.3","1.2.3/evil","1.2","01.2.3","1.2.3-beta","1.2３.4",123,None):
            with self.subTest(version=version):
                self.manifest["version"]=version; self.write_manifest()
                with self.assertRaisesRegex(ValueError,"版本号"):builder.build_package(self.root,self.output)
        self.assertFalse(self.output.exists())

    def test_manifest_must_be_an_object(self):
        (self.root/"_manifest.json").write_text("[]",encoding="utf-8")
        with self.assertRaisesRegex(ValueError,"JSON 对象"):builder.build_package(self.root,self.output)

    def test_icon_path_must_exist_inside_whitelist(self):
        for name,error in (("../secret.png",ValueError),("/secret.png",ValueError),
                           ("C:\\secret.png",ValueError),("missing.png",FileNotFoundError)):
            with self.subTest(name=name):
                self.manifest["display"]["icon"]["value"]=name; self.write_manifest()
                with self.assertRaises(error):builder.build_package(self.root,self.output)
        (self.root/"extra.png").write_bytes(b"image")
        self.manifest["display"]["icon"]["value"]="extra.png"; self.write_manifest()
        with self.assertRaisesRegex(ValueError,"白名单"):builder.build_package(self.root,self.output)

    def test_webui_entry_cannot_escape(self):
        self.webui["pages"][0]["entry"]="webui/../../secret.html"
        (self.root/"webui.json").write_text(json.dumps(self.webui),encoding="utf-8")
        with self.assertRaisesRegex(ValueError,"目录内"):builder.build_package(self.root,self.output)

    def test_symbolic_link_resource_is_rejected(self):
        source=self.root/"mailife.png"; source.unlink()
        outside=Path(self.temporary.name)/"outside.png"; outside.write_bytes(b"private")
        try:source.symlink_to(outside)
        except OSError as exc:self.skipTest(f"当前环境不允许创建符号链接：{exc}")
        with self.assertRaisesRegex(ValueError,"链接"):builder.build_package(self.root,self.output)

    def test_invalid_python_is_rejected_before_output(self):
        (self.root/"plugin.py").write_text("def broken(\n",encoding="utf-8")
        with self.assertRaises(SyntaxError):builder.build_package(self.root,self.output)
        self.assertFalse(self.output.exists())

    def test_zip_write_failure_preserves_old_package_and_cleans_temporary(self):
        old=self.old_package()
        with patch.object(builder.ZipFile,"writestr",side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError,"disk full"):builder.build_package(self.root,self.output)
        self.assertEqual(old.read_bytes(),b"previous release")
        self.assertEqual(list(self.output.glob("*.tmp")),[])


if __name__=="__main__":unittest.main()
