from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import yaml
from pydantic import JsonValue, TypeAdapter

ROOT = Path(__file__).resolve().parent


def model_ids(output: str) -> tuple[str, ...]:
    models = tuple(dict.fromkeys(
        line.split("\t", 1)[0].strip() for line in output.splitlines()
        if "\t" in line and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+", line.split("\t", 1)[0].strip())
    ))
    if not models:
        raise ValueError("agy models returned no tab-separated model IDs")
    return models


def vertex_names(path: Path) -> tuple[str, ...]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return tuple(entry["model_name"] for entry in data["model_list"])


def agy_targets(agy: tuple[str, ...], vertex: tuple[str, ...]) -> dict[str, str]:
    return {name: name for name in agy} | {
        name: name + "-medium" for name in vertex
        if not re.search(r"-(?:low|medium|high|xhigh)$", name) and name + "-medium" in agy
    }


def build_config(agy: tuple[str, ...], vertex: tuple[str, ...], template: Path) -> dict[str, JsonValue]:
    config = TypeAdapter(dict[str, JsonValue]).validate_python(yaml.safe_load(template.read_text(encoding="utf-8")))
    targets = agy_targets(agy, vertex)
    config["model_list"] = [
        {"model_name": name, "litellm_params": {"model": f"antigravity/{targets[name]}"}}
        if name in targets else vertex_entry(name, name)
        for name in dict.fromkeys((*agy, *vertex))
    ] + [vertex_entry("vertex-fallback/" + name, name) for name in targets if name in vertex]
    router = config.get("router_settings")
    if not isinstance(router, dict):
        raise ValueError("Template requires router_settings")
    router["fallbacks"] = [{name: ["vertex-fallback/" + name]} for name in targets if name in vertex]
    return config


def vertex_entry(public: str, upstream: str) -> dict[str, JsonValue]:
    return {
        "model_name": public,
        "litellm_params": {
            "model": "openai/" + upstream, "api_base": "http://127.0.0.1:14604/v1",
            "api_key": "os.environ/VERTEX_PROXY_KEY", "num_retries": 0,
        },
    }


def main(root: Path = ROOT, command: tuple[str, ...] | None = None) -> None:
    target = root / "config.generated.yaml"
    try:
        executable = os.environ.get("ANTIGRAVITY_CLI", str(Path(os.environ["LOCALAPPDATA"]) / "agy/bin/agy.exe"))
        args = TypeAdapter(tuple[str, ...]).validate_json(os.environ.get("ANTIGRAVITY_CLI_ARGS", "[]"))
        result = subprocess.run([*(command or (executable, *args)), "models"], capture_output=True,
                                text=True, encoding="utf-8", timeout=60,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0, check=True)
        agy = model_ids(result.stdout)
        vertex = vertex_names(root.parent / ".vertex/config.yaml")
        config = build_config(agy, vertex, root / "config.template.yaml")
        targets = agy_targets(agy, vertex)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
        temporary.replace(target)
        (root / "models.generated.json").write_text(json.dumps({
            name: f"agy:{targets[name]}" + ("-with-vertex-fallback" if name in vertex else "")
            if name in targets else "vertex"
            for name in dict.fromkeys((*agy, *vertex))
        }, indent=2), encoding="utf-8")
        print(f"Generated {len(agy)} agy models, {sum(name != target for name, target in targets.items())} medium aliases, "
              f"{len(set(vertex) - set(targets))} Vertex-only names, "
              f"{len(set(targets) & set(vertex))} fallbacks")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        if not target.is_file():
            raise RuntimeError("Model discovery failed and no last good config exists") from error
        print(f"WARNING: retaining last good generated config: {type(error).__name__}: {error}")


if __name__ == "__main__":
    main()
