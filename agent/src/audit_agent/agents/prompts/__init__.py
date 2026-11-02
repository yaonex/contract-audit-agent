"""Prompt templates and resource loading."""
import importlib.resources


def load_prompt(name: str) -> str:
    """Load a prompt markdown file by name or version (e.g. 'reentrancy.v1')."""
    filename = name if name.endswith(".md") else f"{name}.md"
    try:
        ref = importlib.resources.files("audit_agent.agents.prompts") / filename
        if not ref.is_file():
            raise FileNotFoundError(f"prompt file not found: {filename}")
        return ref.read_text(encoding="utf-8")
    except (TypeError, ModuleNotFoundError) as err:
        raise FileNotFoundError(f"cannot access prompts package: {err}") from err
