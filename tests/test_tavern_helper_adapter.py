from __future__ import annotations

import json

from memory_core.adapters.tavern_helper import import_tavern_helper_export


def test_attached_style_remote_loader_is_quarantined_and_never_fetched():
    payload = {
        "type": "script",
        "enabled": True,
        "name": "数据库本体",
        "id": "2b83a493-e288-4da0-92f5-4a7e64f62fe1",
        "content": (
            "import 'https://gcore.jsdelivr.net/gh/AlbusKen/shujuku@spv8.9.1/index.js'"
        ),
        "info": "version loader",
        "button": {"enabled": True, "buttons": []},
        "data": {},
        "export_with": {"data": True, "button": True},
    }

    imported = import_tavern_helper_export(json.dumps(payload, ensure_ascii=False))

    assert len(imported.scripts) == 1
    script = imported.scripts[0]
    assert script.quarantined is True
    assert script.code_kind == "remote-loader"
    assert script.dependencies[0].declared_ref == "spv8.9.1"
    assert script.dependencies[0].immutable_ref is False
    assert imported.remote_fetches == 0
    assert imported.code_executed is False
    assert imported.candidates == ()


def test_modern_folder_and_legacy_script_data_become_untrusted_candidates():
    payload = [
        {
            "type": "folder",
            "name": "角色状态",
            "scripts": [
                {
                    "type": "script",
                    "id": "modern",
                    "name": "状态",
                    "content": "console.log('not executed')",
                    "data": {"Alice": {"affection": 7}},
                    "button": {"buttons": [{"name": "刷新"}]},
                }
            ],
        },
        {
            "enabled": False,
            "id": "legacy",
            "name": "旧脚本",
            "content": "",
            "buttons": [{"name": "旧按钮", "visible": True}],
            "data": {"scene": "library"},
        },
    ]

    imported = import_tavern_helper_export(payload, suggested_scope="character")

    assert [item.source_format for item in imported.scripts] == ["modern", "legacy"]
    assert imported.scripts[0].folder_path == ("角色状态",)
    assert imported.scripts[0].buttons == ("刷新",)
    assert {(item.source_id, item.json_pointer) for item in imported.candidates} == {
        ("modern", "/Alice/affection"),
        ("legacy", "/scene"),
    }
    assert all(item.authority == "none" for item in imported.candidates)
    assert all(item.suggested_scope == "character" for item in imported.candidates)


def test_legacy_wrapped_tree_and_secret_values_are_handled_without_disclosure():
    payload = {
        "scriptsRepository": [
            {
                "type": "script",
                "value": {
                    "id": "wrapped",
                    "name": "wrapped",
                    "content": "",
                    "buttons": [],
                    "data": {
                        "profile": "friendly",
                        "api_key": "sk-proj-abcdefghijklmnopqrstuvwxyz",
                    },
                },
            }
        ]
    }

    imported = import_tavern_helper_export(payload)

    assert imported.scripts[0].source_format == "legacy-wrapped"
    assert [item.json_pointer for item in imported.candidates] == ["/profile"]
    assert imported.secret_values_skipped == 1
    assert not any("sk-proj" in warning for warning in imported.warnings)


def test_character_card_settings_and_variables_are_imported_separately():
    character_card = {
        "name": "Alice",
        "data": {
            "extensions": {
                "tavern_helper": {
                    "scripts": [],
                    "variables": {"relationship": {"affection": 12}},
                }
            }
        },
    }

    imported = import_tavern_helper_export(character_card)

    assert imported.scripts == ()
    assert len(imported.variable_bundles) == 1
    assert imported.variable_bundles[0].source_kind == "character_variables"
    assert imported.variable_bundles[0].suggested_scope == "character"
    assert [(item.source_kind, item.json_pointer) for item in imported.candidates] == [
        ("character_variables", "/relationship/affection")
    ]
    assert imported.candidates[0].authority == "none"
