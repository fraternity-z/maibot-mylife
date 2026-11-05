"""隔离宿主 SDK，使用真实插件实现运行离线测试。"""
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from pydantic import BaseModel, Field
import pytest


@pytest.fixture(scope="session")
def plugin_module():
    sdk=ModuleType("maibot_sdk")
    sdk.Field=Field
    sdk.PluginConfigBase=BaseModel
    sdk.MaiBotPlugin=type("MaiBotPlugin",(),{})

    def component(*args,**kwargs):
        def decorate(function):
            function.__maibot_component_info__=SimpleNamespace(command_pattern=kwargs.get("pattern",""))
            return function
        return decorate

    for name in ("API","Command","HomeCard","HookHandler","Tool"):
        setattr(sdk,name,component)
    sdk_types=ModuleType("maibot_sdk.types")
    sdk_types.HookMode=SimpleNamespace(BLOCKING="blocking",OBSERVE="observe")
    sdk_types.HookOrder=SimpleNamespace(EARLY=0)
    sdk_types.ToolParamType=SimpleNamespace(STRING="string",INTEGER="integer")
    sdk_types.ToolParameterInfo=lambda **kwargs:SimpleNamespace(**kwargs)
    package=ModuleType("mai_integration_tests")
    package.__path__=[str(Path(__file__).resolve().parents[1])]
    with patch.dict(sys.modules,{"maibot_sdk":sdk,"maibot_sdk.types":sdk_types,
                                 "mai_integration_tests":package}):
        module=importlib.import_module("mai_integration_tests.plugin")
        yield module
