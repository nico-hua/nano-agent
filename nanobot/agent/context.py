"""Build the system prompt from the current workspace context files."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from ..providers import BaseMessage, HumanMessage, SystemMessage
from ..skills import SkillLoadError, SkillNotFoundError, SkillsLoader

logger = logging.getLogger(__name__)

_BASE_PROMPT = "You are Nanobot, a helpful AI assistant."
_CONTEXT_FILES = (
    ("AGENTS.md", "## Workspace Instructions"),
    ("SOUL.md", "## Agent Style"),
    ("USER.md", "## User Profile"),
)
_SUBAGENT_SYSTEM_PROMPT_TEMPLATE = """# Subagent

You are a subagent spawned by the main agent to complete a specific task.
Stay focused on the assigned task. Your final response will be reported back to the main agent.

## Workspace

Current project workspace: {workspace}

## Skills

{skills_summary}"""


def history_summary_message(summary: str) -> HumanMessage:
    return HumanMessage(content=f"以下是之前的历史摘要：\n{summary}")


class ContextBuilder:
    """Build a fresh system prompt using workspace files, memory, and Skills."""

    def __init__(self, workspace: str | Path) -> None:
        if not isinstance(workspace, (str, Path)):
            raise TypeError("ContextBuilder workspace must be a string or Path")
        self._workspace = Path(workspace).resolve()
        self._skills_loader = SkillsLoader(self._workspace)

    def build_system_prompt(self, *, user_content: str = "", memory: str = "") -> str:
        """Return a prompt built from the current workspace file contents."""

        sections = [
            "\n".join(
                (
                    "# Nanobot",
                    "",
                    _BASE_PROMPT,
                    "",
                    "## Workspace",
                    "",
                    f"`{self._workspace}`",
                )
            )
        ]
        for filename, title in _CONTEXT_FILES:
            content = self._read_optional_file(filename)
            if content is not None:
                sections.append(f"{title}\n\n{content}")
        sections.extend(self._build_skill_sections())
        if user_content:
            active_skills = self._build_active_skill_context(user_content)
            if active_skills:
                sections.append(active_skills)
        if memory:
            sections.append(f"## Long-term Memory\n\n{memory}")
        return "\n\n".join(sections)

    def build_subagent_system_prompt(self) -> str:
        """Build the isolated system prompt used for one subagent task."""

        return _SUBAGENT_SYSTEM_PROMPT_TEMPLATE.format(
            workspace=self._workspace,
            skills_summary="\n\n".join(self._build_skill_sections()),
        )

    def build_request_messages(
        self,
        history: Sequence[BaseMessage],
        current_message: HumanMessage,
        *,
        summary: str | None = None,
        memory: str = "",
    ) -> tuple[BaseMessage, ...]:
        """Place the optional summary before the verbatim history and current request."""

        _validate_messages(history)
        if not isinstance(current_message, HumanMessage):
            raise TypeError("ContextBuilder current_message must be a HumanMessage")
        system = SystemMessage(content=self.build_system_prompt(user_content=current_message.content, memory=memory))
        if summary:
            return (system, history_summary_message(summary), *history, current_message)
        return (system, *history, current_message)

    def _read_optional_file(self, filename: str) -> str | None:
        try:
            content = (self._workspace / filename).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError):
            logger.warning("Skipping unreadable workspace context file (name=%s)", filename)
            return None
        return content if content.strip() else None

    def _build_skill_sections(self) -> tuple[str, ...]:
        """Build static Skill context without executing or persisting Skill contents."""

        try:
            skills = self._skills_loader.list_skills()
        except Exception:
            logger.exception("Skipping Skills after discovery failed")
            return ()

        sections: list[str] = []
        always_active = []
        for skill in skills:
            if not skill.always or not skill.is_available:
                continue
            try:
                content = self._skills_loader.read_skill(skill.name)
            except (SkillLoadError, SkillNotFoundError):
                logger.warning("Skipping unavailable always-active Skill (name=%s)", skill.name)
                continue
            except Exception:
                logger.exception("Skipping always-active Skill after loading failed (name=%s)", skill.name)
                continue
            if content.strip():
                always_active.append(f"### {skill.name}\n\n{content}")
        if always_active:
            sections.append("## Always-active Skills\n\n" + "\n\n".join(always_active))

        available_skills = [
            skill for skill in skills if not skill.always and skill.is_available
        ]
        if available_skills:
            entries = [
                "- "
                f"**{skill.name}**: {skill.description or 'No description provided.'} "
                f"(`{skill.path}`)"
                for skill in available_skills
            ]
            sections.append("## Available Skills\n\n" + "\n".join(entries))
        unavailable_skills = [skill for skill in skills if not skill.is_available]
        if unavailable_skills:
            entries = [
                "- "
                f"**{skill.name}**: {skill.description or 'No description provided.'} "
                f"Unavailable; missing {', '.join(f'`{dependency}`' for dependency in skill.missing_dependencies)}. "
                f"(`{skill.path}`)"
                for skill in unavailable_skills
            ]
            sections.append("## Unavailable Skills\n\n" + "\n".join(entries))
        return tuple(sections)

    def _build_active_skill_context(self, content: str) -> str | None:
        """Load explicitly referenced non-always Skills for this request only."""

        try:
            referenced_skills = self._skills_loader.find_referenced_skills(content)
        except Exception:
            logger.exception("Skipping explicit Skills after reference discovery failed")
            return None

        active_skills = []
        unavailable_skills = []
        for skill in referenced_skills:
            if not skill.is_available:
                unavailable_skills.append(
                    f"- **{skill.name}** is unavailable: missing "
                    + ", ".join(
                        f"`{dependency}`" for dependency in skill.missing_dependencies
                    )
                    + "."
                )
                continue
            if skill.always:
                continue
            try:
                skill_content = self._skills_loader.read_skill(skill.name)
            except (SkillLoadError, SkillNotFoundError):
                logger.warning("Skipping unavailable explicit Skill (name=%s)", skill.name)
                continue
            except Exception:
                logger.exception("Skipping explicit Skill after loading failed (name=%s)", skill.name)
                continue
            if skill_content.strip():
                active_skills.append(f"### {skill.name}\n\n{skill_content}")
        sections: list[str] = []
        if active_skills:
            sections.append(
                "[Active Skills for this turn]\n\n"
                + "\n\n".join(active_skills)
                + "\n\n[/Active Skills]"
            )
        if unavailable_skills:
            sections.append(
                "[Unavailable Skills requested for this turn]\n\n"
                + "\n".join(unavailable_skills)
                + "\n\n[/Unavailable Skills]"
            )
        if not sections:
            return None
        return "\n\n".join(sections)


def _validate_messages(messages: Sequence[BaseMessage]) -> None:
    if not isinstance(messages, Sequence) or not all(
        isinstance(message, BaseMessage) for message in messages
    ):
        raise TypeError("messages must be a sequence of BaseMessage instances")
