"""Tool for loading skill content on demand."""

from typing import Any

from nanobot.agent.skills import SkillsLoader
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema


@tool_parameters(
    tool_parameters_schema(
        skill_name=StringSchema("Name of the skill to load (from the skills list)"),
        required=["skill_name"],
    )
)
class LoadSkillTool(Tool):
    """Load a skill's SKILL.md content by name.

    Returns the file path and body (frontmatter stripped) so the agent can
    follow skill instructions and resolve relative paths to sub-resources.
    """

    def __init__(self, skills_loader: SkillsLoader):
        self._loader = skills_loader

    @property
    def name(self) -> str:
        return "load_skill"

    @property
    def description(self) -> str:
        return (
            "Load a skill by name to get its full instructions. "
            "Returns the SKILL.md file path and body content. "
            "Use this when you need to follow a skill's instructions."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, skill_name: str, **kwargs: Any) -> str:
        path = self._loader.get_skill_path(skill_name)
        if path is None:
            available = [
                entry["name"] for entry in self._loader.list_skills(filter_unavailable=False)
            ]
            names = ", ".join(available) if available else "(none)"
            return f"Error: Skill '{skill_name}' not found. Available skills: {names}"

        content = path.read_text(encoding="utf-8")
        body = self._loader.strip_frontmatter(content)
        return f"File: {path}\n\n{body}"
