"""部署配置与消息边界的回归检查。"""
from pathlib import Path
import tomllib

import pytest


def test_shipped_template_loads_with_current_schema(plugin_module):
    data=tomllib.loads((Path(__file__).parents[1]/"config.toml.example").read_text(encoding="utf-8"))
    settings=plugin_module.MaiLifeSettings.model_validate(data)
    assert settings.plugin.config_version=="1.15.0"
    assert settings.rest_gate.work_start==settings.rest_gate.work_end=="00:00"
    assert settings.group.gate_enabled and settings.group.reply_probability==0.18
    assert "reply_probability" not in type(settings.rest_gate).model_fields
    assert "state" not in data and "recall" not in data


def test_removed_simulation_settings_are_ignored_in_legacy_config(plugin_module):
    settings=plugin_module.MaiLifeSettings.model_validate({
        "state":{"body_cycle_enabled":True,"awake_grace_minutes":30},
        "recall":{"enabled":True},
        "memory":{"dream_probability":1,"dream_fragments_enabled":True},
        "models":{"dream_task":"old-dream-task"},
        "creation":{"minimum_energy":99},
        "proactive":{"minimum_energy":99},
    })
    data=settings.model_dump()
    assert "state" not in data and "recall" not in data
    assert not any(key.startswith("dream_") for key in data["memory"])
    assert "dream_task" not in data["models"]
    assert "minimum_energy" not in data["creation"] and "minimum_energy" not in data["proactive"]
    assert data["memory"]["diary_enabled"] and data["memory"]["important_dates_enabled"]


@pytest.mark.parametrize("info",[None,"invalid",{"additional_config":"invalid"}])
def test_admission_handles_invalid_adapter_metadata(plugin_module,info):
    message={"message_info":info}
    result=plugin_module.MaiLifePlugin._probability_pass({},message,True)
    assert result["modified_kwargs"]["message"]["message_info"]["additional_config"]["is_mentioned"]==1.0
