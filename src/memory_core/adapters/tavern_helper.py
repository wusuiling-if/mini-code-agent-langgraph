"""Safe, non-executing importer for Tavern Helper (酒馆助手) exports.

Tavern Helper is intentionally a JavaScript runtime.  This adapter is not: it
parses portable script metadata and declarative data, inventories literal remote
dependencies, and emits review candidates.  Script content is always quarantined.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from memory_core.security import SecretDetector

_STATIC_IMPORT = re.compile(
    r"(?m)^\s*import\s+(?:(?:[^'\";]+?)\s+from\s+)?['\"](https?://[^'\"]+)['\"]\s*;?"
)
_DYNAMIC_IMPORT = re.compile(r"\bimport\s*\(\s*['\"](https?://[^'\"]+)['\"]\s*\)")
_JSDELIVR_GH_REF = re.compile(r"/(?:gh/)?[^/]+/[^/@]+@([^/]+)/")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RemoteScriptDependency:
    url: str
    host: str
    declared_ref: str | None
    immutable_ref: bool
    import_style: str


@dataclass(frozen=True)
class TavernHelperDataCandidate:
    """A declarative leaf that still requires schema mapping and admission."""

    source_id: str
    source_kind: str
    json_pointer: str
    value_json: str
    source_sha256: str
    suggested_scope: str
    authority: str = "none"


@dataclass(frozen=True)
class TavernHelperScriptArtifact:
    script_id: str
    name: str
    enabled: bool
    info: str
    folder_path: tuple[str, ...]
    source_format: str
    source_sha256: str
    content_sha256: str
    data_sha256: str
    data_json: str
    buttons: tuple[str, ...]
    code_kind: str
    dependencies: tuple[RemoteScriptDependency, ...]
    quarantined: bool = True


@dataclass(frozen=True)
class TavernHelperVariableBundle:
    source_id: str
    source_kind: str
    source_sha256: str
    data_sha256: str
    data_json: str
    suggested_scope: str


@dataclass(frozen=True)
class TavernHelperImport:
    scripts: tuple[TavernHelperScriptArtifact, ...]
    variable_bundles: tuple[TavernHelperVariableBundle, ...]
    candidates: tuple[TavernHelperDataCandidate, ...]
    warnings: tuple[str, ...]
    rejected_items: int
    secret_values_skipped: int
    remote_fetches: int = 0
    code_executed: bool = False


def import_tavern_helper_export(
    payload: str | Mapping[str, Any] | Sequence[object],
    *,
    suggested_scope: str = "conversation",
    max_input_bytes: int = 16 * 1024 * 1024,
    max_scripts: int = 10_000,
    max_script_chars: int = 4_000_000,
    max_candidates: int = 100_000,
    max_data_depth: int = 32,
) -> TavernHelperImport:
    """Parse modern, folder, legacy, and settings-wrapped exports safely.

    The returned candidates have ``authority='none'``.  Hosts must explicitly
    map JSON pointers to a memory schema and run their normal evidence admission
    before any candidate becomes durable memory.
    """

    root = _load_payload(payload, max_input_bytes=max_input_bytes)
    settings, detected_scope, settings_kind = _settings_payload(root, suggested_scope)
    items = _locate_script_trees(settings, allow_empty=True)
    scripts: list[TavernHelperScriptArtifact] = []
    variable_bundles: list[TavernHelperVariableBundle] = []
    candidates: list[TavernHelperDataCandidate] = []
    warnings: list[str] = []
    rejected = 0
    skipped_secrets = 0
    detector = SecretDetector()

    def walk(item: object, folder_path: tuple[str, ...] = ()) -> None:
        nonlocal rejected, skipped_secrets
        if len(scripts) >= max_scripts:
            raise ValueError("Tavern Helper export exceeds the script limit")
        if not isinstance(item, Mapping):
            rejected += 1
            warnings.append("ignored a non-object script tree item")
            return

        item_type = str(item.get("type", ""))
        if item_type == "folder":
            folder_name = str(item.get("name", "")).strip() or "unnamed-folder"
            children = item.get("scripts", item.get("value", []))
            if not isinstance(children, list):
                rejected += 1
                warnings.append(f"folder {folder_name!r} has no valid script list")
                return
            for child in children:
                walk(child, (*folder_path, folder_name))
            return

        source_format = "modern"
        script: Mapping[str, Any] = item
        if item_type == "script" and isinstance(item.get("value"), Mapping):
            script = item["value"]
            source_format = "legacy-wrapped"
        elif not item_type and "buttons" in item:
            source_format = "legacy"
        elif item_type not in {"", "script"}:
            rejected += 1
            warnings.append(f"ignored unsupported tree item type {item_type!r}")
            return

        content = script.get("content", "")
        if not isinstance(content, str):
            rejected += 1
            warnings.append("ignored a script whose content is not text")
            return
        if len(content) > max_script_chars:
            raise ValueError("Tavern Helper script exceeds the character limit")
        data = script.get("data", {})
        if not isinstance(data, Mapping):
            rejected += 1
            warnings.append("ignored a script whose data field is not an object")
            return

        source_sha256 = _digest(item)
        raw_id = script.get("id", "")
        script_id = str(raw_id).strip() or f"sha256:{source_sha256[:24]}"
        name = str(script.get("name", "")).strip() or "unnamed-script"
        dependencies = _dependencies(content)
        code_kind = _code_kind(content, dependencies)
        button_value = script.get("button")
        if isinstance(button_value, Mapping):
            raw_buttons = button_value.get("buttons", [])
        else:
            raw_buttons = script.get("buttons", [])
        buttons = (
            tuple(
                str(button.get("name", ""))
                for button in raw_buttons
                if isinstance(button, Mapping) and str(button.get("name", "")).strip()
            )
            if isinstance(raw_buttons, list)
            else ()
        )
        data_json = _canonical_json(dict(data))
        artifact = TavernHelperScriptArtifact(
            script_id=script_id,
            name=name,
            enabled=bool(script.get("enabled", False)),
            info=str(script.get("info", "")),
            folder_path=folder_path,
            source_format=source_format,
            source_sha256=source_sha256,
            content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            data_sha256=hashlib.sha256(data_json.encode("utf-8")).hexdigest(),
            data_json=data_json,
            buttons=buttons,
            code_kind=code_kind,
            dependencies=dependencies,
        )
        scripts.append(artifact)
        if content.strip():
            warnings.append(f"script {name!r} contains quarantined {code_kind}")
        for pointer, value in _data_leaves(data, max_depth=max_data_depth):
            if len(candidates) >= max_candidates:
                raise ValueError("Tavern Helper export exceeds the candidate limit")
            value_json = _canonical_json(value)
            if detector.contains_secret(f"{pointer}={value_json}"):
                skipped_secrets += 1
                continue
            candidates.append(
                TavernHelperDataCandidate(
                    source_id=script_id,
                    source_kind="script_data",
                    json_pointer=pointer,
                    value_json=value_json,
                    source_sha256=source_sha256,
                    suggested_scope=suggested_scope,
                )
            )

    for tree in items:
        walk(tree)
    variables = settings.get("variables") if isinstance(settings, Mapping) else None
    if isinstance(variables, Mapping):
        variables_json = _canonical_json(dict(variables))
        variable_source_sha256 = _digest(
            {"source_kind": settings_kind, "variables": dict(variables)}
        )
        bundle = TavernHelperVariableBundle(
            source_id=f"tavern-helper:{settings_kind}:variables",
            source_kind=settings_kind,
            source_sha256=variable_source_sha256,
            data_sha256=hashlib.sha256(variables_json.encode("utf-8")).hexdigest(),
            data_json=variables_json,
            suggested_scope=detected_scope,
        )
        variable_bundles.append(bundle)
        for pointer, value in _data_leaves(variables, max_depth=max_data_depth):
            if len(candidates) >= max_candidates:
                raise ValueError("Tavern Helper export exceeds the candidate limit")
            value_json = _canonical_json(value)
            if detector.contains_secret(f"{pointer}={value_json}"):
                skipped_secrets += 1
                continue
            candidates.append(
                TavernHelperDataCandidate(
                    source_id=bundle.source_id,
                    source_kind=settings_kind,
                    json_pointer=pointer,
                    value_json=value_json,
                    source_sha256=variable_source_sha256,
                    suggested_scope=detected_scope,
                )
            )
    if skipped_secrets:
        warnings.append(
            f"skipped {skipped_secrets} suspected secret value(s) from candidates"
        )
    return TavernHelperImport(
        scripts=tuple(scripts),
        variable_bundles=tuple(variable_bundles),
        candidates=tuple(candidates),
        warnings=tuple(warnings),
        rejected_items=rejected,
        secret_values_skipped=skipped_secrets,
    )


def _load_payload(
    payload: str | Mapping[str, Any] | Sequence[object], *, max_input_bytes: int
) -> object:
    if isinstance(payload, str):
        if len(payload.encode("utf-8")) > max_input_bytes:
            raise ValueError("Tavern Helper export exceeds the input byte limit")
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid Tavern Helper JSON export") from exc
    return payload


def _settings_payload(root: object, suggested_scope: str) -> tuple[object, str, str]:
    if not isinstance(root, Mapping):
        return root, suggested_scope, "script_export"
    data = root.get("data")
    if isinstance(data, Mapping):
        extensions = data.get("extensions")
        if isinstance(extensions, Mapping):
            current = extensions.get("tavern_helper")
            if isinstance(current, Mapping):
                return current, "character", "character_variables"
            old_scripts = extensions.get("TavernHelper_scripts")
            old_variables = extensions.get("TavernHelper_characterScriptVariables")
            if isinstance(old_scripts, list) or isinstance(old_variables, Mapping):
                return (
                    {
                        "scripts": old_scripts if isinstance(old_scripts, list) else [],
                        "variables": (
                            old_variables if isinstance(old_variables, Mapping) else {}
                        ),
                    },
                    "character",
                    "legacy_character_variables",
                )
    extensions = root.get("extensions")
    if isinstance(extensions, Mapping) and isinstance(
        extensions.get("tavern_helper"), Mapping
    ):
        return extensions["tavern_helper"], "preset", "preset_variables"
    return root, suggested_scope, "settings_variables"


def _locate_script_trees(root: object, *, allow_empty: bool = False) -> list[object]:
    if isinstance(root, list):
        return list(root)
    if not isinstance(root, Mapping):
        raise TypeError("Tavern Helper export must be a JSON object or array")
    if root.get("type") in {"script", "folder"} or "content" in root:
        return [root]
    candidates: object | None = None
    script_settings = root.get("script")
    if isinstance(script_settings, Mapping):
        candidates = script_settings.get(
            "scripts", script_settings.get("scriptsRepository")
        )
    for key in ("script_trees", "scriptsRepository", "scripts"):
        if candidates is None and key in root:
            candidates = root[key]
    if (
        candidates is None
        and allow_empty
        and isinstance(root.get("variables"), Mapping)
    ):
        return []
    if not isinstance(candidates, list):
        raise TypeError("no Tavern Helper script tree list found in export")
    return list(candidates)


def _dependencies(content: str) -> tuple[RemoteScriptDependency, ...]:
    observed: dict[str, str] = {}
    for match in _STATIC_IMPORT.finditer(content):
        observed.setdefault(match.group(1), "static")
    for match in _DYNAMIC_IMPORT.finditer(content):
        observed.setdefault(match.group(1), "dynamic")
    dependencies = []
    for url, style in observed.items():
        parsed = urlparse(url)
        ref_match = _JSDELIVR_GH_REF.search(parsed.path)
        declared_ref = ref_match.group(1) if ref_match else None
        dependencies.append(
            RemoteScriptDependency(
                url=url,
                host=parsed.hostname or "",
                declared_ref=declared_ref,
                immutable_ref=bool(
                    declared_ref and re.fullmatch(r"[0-9a-fA-F]{40}", declared_ref)
                ),
                import_style=style,
            )
        )
    return tuple(dependencies)


def _code_kind(content: str, dependencies: Sequence[RemoteScriptDependency]) -> str:
    if not content.strip():
        return "empty"
    stripped = _STATIC_IMPORT.sub("", content)
    stripped = re.sub(r"(?m)^\s*//.*$", "", stripped).strip(" \t\r\n;")
    if dependencies and not stripped:
        return "remote-loader"
    if dependencies:
        return "javascript-with-remote-imports"
    return "inline-javascript"


def _data_leaves(
    value: object, *, max_depth: int, pointer: str = "", depth: int = 0
) -> list[tuple[str, object]]:
    if depth > max_depth:
        raise ValueError("Tavern Helper data exceeds the nesting limit")
    leaves: list[tuple[str, object]] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            token = str(key).replace("~", "~0").replace("/", "~1")
            leaves.extend(
                _data_leaves(
                    child,
                    max_depth=max_depth,
                    pointer=f"{pointer}/{token}",
                    depth=depth + 1,
                )
            )
        return leaves
    if isinstance(value, list):
        for index, child in enumerate(value):
            leaves.extend(
                _data_leaves(
                    child,
                    max_depth=max_depth,
                    pointer=f"{pointer}/{index}",
                    depth=depth + 1,
                )
            )
        return leaves
    return [(pointer or "/", value)]
