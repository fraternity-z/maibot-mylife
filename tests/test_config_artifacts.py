"""部署配置与消息边界的回归检查。"""
from pathlib import Path
import tomllib

import pytest
from pydantic import ValidationError


def test_shipped_template_loads_with_current_schema(plugin_module):
    data=tomllib.loads((Path(__file__).parents[1]/"config.toml.example").read_text(encoding="utf-8"))
    settings=plugin_module.MaiLifeSettings.model_validate(data)
    assert settings.plugin.config_version=="1.16.0"
    assert settings.users.profiles[0].daily_proactive_unlimited is False
    assert settings.users.profiles[0].daily_proactive_max==1
    assert settings.proactive.patrol_interval_minutes==180
    assert "min_interval_minutes" not in data["proactive"]
    assert settings.rest_gate.work_start==settings.rest_gate.work_end=="00:00"
    assert settings.group.gate_enabled and settings.group.reply_probability==0.18
    assert "reply_probability" not in type(settings.rest_gate).model_fields
    assert "state" not in data and "recall" not in data


@pytest.mark.parametrize("minutes",[1,10,60,1440])
def test_planner_interval_keeps_existing_config_key(plugin_module,minutes):
    settings=plugin_module.MaiLifeSettings.model_validate({"proactive":{"patrol_interval_minutes":minutes}})
    assert settings.proactive.patrol_interval_minutes==minutes
    assert settings.model_dump()["proactive"]["patrol_interval_minutes"]==minutes
    assert "min_interval_minutes" not in settings.model_dump()["proactive"]


@pytest.mark.parametrize("minutes",[0,1441])
def test_planner_interval_rejects_out_of_range_values(plugin_module,minutes):
    with pytest.raises(ValidationError):
        plugin_module.MaiLifeSettings.model_validate({"proactive":{"patrol_interval_minutes":minutes}})


@pytest.mark.parametrize("fields,expected",[
    ({},180),({"patrol_interval_minutes":10,"min_interval_minutes":180},180),
    ({"patrol_interval_minutes":60,"min_interval_minutes":30},60),
    ({"min_interval_minutes":30},30),({"patrol_interval_minutes":30},30),
    ({"patrol_interval_minutes":1,"min_interval_minutes":1440},1440),
])
def test_legacy_intervals_merge_without_mutating_input(plugin_module,fields,expected):
    original=dict(fields)
    settings=plugin_module.MaiLifeSettings.model_validate({"proactive":fields})
    proactive=settings.model_dump()["proactive"]
    assert proactive["patrol_interval_minutes"]==expected
    assert "min_interval_minutes" not in proactive
    assert fields==original
    reloaded=plugin_module.MaiLifeSettings.model_validate(settings.model_dump())
    assert reloaded.proactive.patrol_interval_minutes==expected


@pytest.mark.parametrize("fields",[
    {"min_interval_minutes":0},{"min_interval_minutes":1441},{"min_interval_minutes":"invalid"},
    {"min_interval_minutes":1.5},{"min_interval_minutes":None},
    {"patrol_interval_minutes":180,"min_interval_minutes":0},
    {"patrol_interval_minutes":0,"min_interval_minutes":180},
    {"patrol_interval_minutes":"invalid","min_interval_minutes":180},
    {"patrol_interval_minutes":1.5,"min_interval_minutes":180},
])
def test_invalid_legacy_intervals_are_not_hidden_by_the_other_value(plugin_module,fields):
    with pytest.raises(ValidationError):
        plugin_module.MaiLifeSettings.model_validate({"proactive":fields})


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
