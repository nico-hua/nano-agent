"""Tests for workspace-based system prompt construction."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nanobot.agent import ContextBuilder, estimate_message_tokens
from nanobot.providers import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolCallRequest,
    ToolMessage,
)
from nanobot.session import Session


class ContextBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._workspace = Path(self._temporary_directory.name)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_builds_prompt_with_all_workspace_context_files(self) -> None:
        self._write("AGENTS.md", "Follow the workspace rules.")
        self._write("SOUL.md", "Be concise and curious.")
        self._write("USER.md", "The user prefers Chinese.")

        prompt = ContextBuilder(self._workspace).build_system_prompt()

        self.assertIn("You are Nanobot, a helpful AI assistant.", prompt)
        self.assertIn(f"`{self._workspace.resolve()}`", prompt)
        self.assertIn("## Workspace Instructions\n\nFollow the workspace rules.", prompt)
        self.assertIn("## Agent Style\n\nBe concise and curious.", prompt)
        self.assertIn("## User Profile\n\nThe user prefers Chinese.", prompt)

    def test_skips_missing_and_empty_context_files(self) -> None:
        self._write("AGENTS.md", "")
        self._write("SOUL.md", "   \n")

        prompt = ContextBuilder(self._workspace).build_system_prompt()

        self.assertIn("# Nanobot", prompt)
        self.assertNotIn("## Workspace Instructions", prompt)
        self.assertNotIn("## Agent Style", prompt)
        self.assertNotIn("## User Profile", prompt)

    def test_places_memory_after_workspace_and_skill_sections(self) -> None:
        self._write("AGENTS.md", "Workspace rules.")
        self._write_skill("guide", "---\nname: guide\ndescription: A guide.\n---\nSkill body.\n")
        messages = ContextBuilder(self._workspace).build_request_messages(
            (), HumanMessage(content="Use $guide."), memory="- [decision] Current preference.",
        )

        prompt = messages[0].content
        self.assertLess(prompt.index("Workspace rules."), prompt.index("## Available Skills"))
        self.assertLess(prompt.index("## Available Skills"), prompt.index("Skill body."))
        self.assertTrue(prompt.endswith(
            "[/Active Skills]\n\n## Long-term Memory\n\n- [decision] Current preference."
        ))

    def test_uses_a_stable_context_file_order(self) -> None:
        self._write("AGENTS.md", "agents")
        self._write("SOUL.md", "soul")
        self._write("USER.md", "user")

        prompt = ContextBuilder(self._workspace).build_system_prompt()

        self.assertLess(
            prompt.index("## Workspace Instructions"),
            prompt.index("## Agent Style"),
        )
        self.assertLess(
            prompt.index("## Agent Style"),
            prompt.index("## User Profile"),
        )

    def test_includes_long_term_memory_after_workspace_context(self) -> None:
        self._write("USER.md", "The user prefers Chinese.")

        prompt = ContextBuilder(self._workspace).build_system_prompt(memory="Remember the user prefers concise answers.")

        self.assertIn("## Long-term Memory", prompt)
        self.assertIn("Remember the user prefers concise answers.", prompt)
        self.assertLess(prompt.index("## User Profile"), prompt.index("## Long-term Memory"))

    def test_skips_missing_or_empty_long_term_memory(self) -> None:
        builder = ContextBuilder(self._workspace)

        self.assertNotIn("## Long-term Memory", builder.build_system_prompt())

    def test_uses_only_the_supplied_memory_for_each_system_prompt(self) -> None:
        builder = ContextBuilder(self._workspace)

        first_prompt = builder.build_system_prompt(memory="First memory.")
        second_prompt = builder.build_system_prompt(memory="Second memory.")

        self.assertIn("First memory.", first_prompt)
        self.assertIn("Second memory.", second_prompt)
        self.assertNotIn("First memory.", second_prompt)

    def test_includes_always_skill_bodies_and_available_skill_summaries(self) -> None:
        always_path = self._write_skill(
            "always",
            "---\nname: always-skill\ndescription: Always available.\nalways: true\n---\nAlways body instruction.\n",
        )
        available_path = self._write_skill(
            "available",
            "---\nname: available-skill\ndescription: Available on demand.\n---\nDo not inject this full body.\n",
        )

        prompt = ContextBuilder(self._workspace).build_system_prompt()

        self.assertIn("## Always-active Skills", prompt)
        self.assertIn("### always-skill\n\nAlways body instruction.", prompt)
        self.assertIn("## Available Skills", prompt)
        self.assertIn("**available-skill**: Available on demand.", prompt)
        self.assertIn(f"`{available_path}`", prompt)
        self.assertNotIn("Do not inject this full body.", prompt)
        self.assertNotIn(f"`{always_path}`", prompt)

    def test_skips_empty_skill_sections_when_no_skills_are_available(self) -> None:
        prompt = ContextBuilder(self._workspace).build_system_prompt()

        self.assertNotIn("## Always-active Skills", prompt)
        self.assertNotIn("## Available Skills", prompt)

    def test_builds_subagent_prompt_with_workspace_skill_paths(self) -> None:
        self._write_skill(
            "alpha",
            "---\nname: alpha\nalways: true\n---\nAlpha instructions.\n",
        )
        self._write_skill(
            "beta",
            "---\nname: beta\ndescription: Beta help.\n---\nBeta instructions stay in the file.\n",
        )

        prompt = ContextBuilder(self._workspace).build_subagent_system_prompt()

        self.assertIn("# Subagent", prompt)
        self.assertIn("Current project workspace: ", prompt)
        self.assertIn(str(self._workspace.resolve()), prompt)
        self.assertIn("## Always-active Skills", prompt)
        self.assertIn("Alpha instructions.", prompt)
        self.assertIn("## Available Skills", prompt)
        self.assertIn("**beta**: Beta help.", prompt)
        self.assertNotIn("Beta instructions stay in the file.", prompt)

    def test_builds_subagent_prompt_without_skills(self) -> None:
        prompt = ContextBuilder(self._workspace).build_subagent_system_prompt()

        self.assertIn("## Skills", prompt)
        self.assertNotIn("## Always-active Skills", prompt)
        self.assertNotIn("## Available Skills", prompt)

    def test_reloads_always_skill_content_for_each_system_prompt(self) -> None:
        path = self._write_skill(
            "always",
            "---\nname: always\nalways: true\n---\nFirst instruction.\n",
        )
        builder = ContextBuilder(self._workspace)

        first_prompt = builder.build_system_prompt()
        path.write_text(
            "---\nname: always\nalways: true\n---\nSecond instruction.\n",
            encoding="utf-8",
        )
        second_prompt = builder.build_system_prompt()

        self.assertIn("First instruction.", first_prompt)
        self.assertIn("Second instruction.", second_prompt)
        self.assertNotIn("First instruction.", second_prompt)

    def test_skill_content_is_not_saved_in_session_messages(self) -> None:
        self._write_skill(
            "always",
            "---\nname: always\nmetadata:\n  nanobot:\n    always: true\n---\nPersistent Skill instruction.\n",
        )
        session = Session.create("skill-test").with_messages(
            (HumanMessage(content="Previous question."),)
        )
        original_messages = session.messages

        messages = ContextBuilder(self._workspace).build_request_messages(
            session.messages,
            HumanMessage(content="Current question."),
            memory="Persistent user preference.",
        )

        self.assertEqual(session.messages, original_messages)
        self.assertIn("Persistent Skill instruction.", messages[0].content)
        self.assertFalse(
            any("Persistent Skill instruction." in message.content for message in session.messages)
        )

    def test_skill_contents_are_never_executed(self) -> None:
        self._write_skill(
            "static",
            "---\nname: static\nalways: true\n---\npython -c \\\"raise RuntimeError('must not run')\\\"\n",
        )

        prompt = ContextBuilder(self._workspace).build_system_prompt()

        self.assertIn("raise RuntimeError('must not run')", prompt)

    def test_lists_skills_in_a_stable_order_and_skips_unreadable_skill_files(self) -> None:
        self._write_skill(
            "zeta-always",
            "---\nname: zeta-always\nalways: true\n---\nZeta instruction.\n",
        )
        self._write_skill(
            "alpha-always",
            "---\nname: alpha-always\nalways: true\n---\nAlpha instruction.\n",
        )
        self._write_skill(
            "zeta-available",
            "---\nname: zeta-available\ndescription: Zeta available.\n---\nZeta body.\n",
        )
        self._write_skill(
            "alpha-available",
            "---\nname: alpha-available\ndescription: Alpha available.\n---\nAlpha body.\n",
        )
        broken_path = self._workspace / "skills" / "broken" / "SKILL.md"
        broken_path.parent.mkdir(parents=True)
        broken_path.write_bytes(b"\xff")

        prompt = ContextBuilder(self._workspace).build_system_prompt()

        self.assertLess(prompt.index("### alpha-always"), prompt.index("### zeta-always"))
        self.assertLess(prompt.index("**alpha-available**"), prompt.index("**zeta-available**"))
        self.assertNotIn("broken", prompt)

    def test_explicit_references_load_non_always_skills_in_message_order_once(self) -> None:
        self._write_skill(
            "github",
            "---\nname: github\ndescription: GitHub help.\n---\nGitHub full instructions.\n",
        )
        self._write_skill(
            "weather",
            "---\nname: weather\ndescription: Weather help.\n---\nWeather full instructions.\n",
        )
        outside_path = self._workspace / "outside" / "SKILL.md"
        outside_path.parent.mkdir(parents=True, exist_ok=True)
        outside_path.write_text("Outside instructions.", encoding="utf-8")

        messages = ContextBuilder(self._workspace).build_request_messages(
            (),
            HumanMessage(
                content="Use $weather, then $github, then $weather again; ignore $unknown and $../outside."
            ),
        )

        prompt = messages[0].content
        self.assertIn("[Active Skills for this turn]", prompt)
        self.assertIn("[/Active Skills]", prompt)
        self.assertLess(
            prompt.index("Weather full instructions."),
            prompt.index("GitHub full instructions."),
        )
        self.assertEqual(prompt.count("Weather full instructions."), 1)
        self.assertNotIn("Outside instructions.", prompt)
        self.assertEqual(messages[-1].content, "Use $weather, then $github, then $weather again; ignore $unknown and $../outside.")

    def test_explicit_reference_does_not_repeat_an_always_active_skill(self) -> None:
        self._write_skill(
            "always",
            "---\nname: always\nalways: true\n---\nAlways Skill instruction.\n",
        )

        messages = ContextBuilder(self._workspace).build_request_messages(
            (),
            HumanMessage(content="Please use $always for this request."),
        )

        prompt = messages[0].content
        self.assertIn("## Always-active Skills", prompt)
        self.assertEqual(prompt.count("Always Skill instruction."), 1)
        self.assertNotIn("[Active Skills for this turn]", prompt)

    @patch("nanobot.skills.loader.shutil.which", return_value=None)
    def test_marks_unavailable_skills_and_does_not_activate_them(self, which: object) -> None:
        self._write_skill(
            "github",
            "---\nname: github\ndescription: GitHub help.\nnanobot:\n  requires:\n    bins: [\"gh\"]\n---\nGitHub full instructions.\n",
        )

        messages = ContextBuilder(self._workspace).build_request_messages(
            (),
            HumanMessage(content="Use $github."),
        )

        prompt = messages[0].content
        self.assertIn("## Unavailable Skills", prompt)
        self.assertIn("**github**", prompt)
        self.assertIn("`bin: gh`", prompt)
        self.assertNotIn("## Available Skills", prompt)
        self.assertNotIn("GitHub full instructions.", prompt)
        self.assertNotIn("[Active Skills for this turn]", prompt)
        self.assertIn("[Unavailable Skills requested for this turn]", prompt)
        self.assertIn("**github** is unavailable: missing `bin: gh`.", prompt)

    def test_explicit_skill_context_only_affects_its_current_request(self) -> None:
        self._write_skill(
            "github",
            "---\nname: github\n---\nGitHub request-only instructions.\n",
        )
        builder = ContextBuilder(self._workspace)
        session = Session.create("skill-request").with_messages(
            (HumanMessage(content="Previous question."),)
        )
        original_messages = session.messages

        active_request = builder.build_request_messages(
            session.messages,
            HumanMessage(content="Use $github."),
        )
        normal_request = builder.build_request_messages(
            session.messages,
            HumanMessage(content="Use normal behavior."),
        )

        self.assertIn("GitHub request-only instructions.", active_request[0].content)
        self.assertNotIn("GitHub request-only instructions.", normal_request[0].content)
        self.assertEqual(session.messages, original_messages)
        self.assertEqual(active_request[-1], HumanMessage(content="Use $github."))

    def test_unknown_or_invalid_skill_references_leave_request_behavior_unchanged(self) -> None:
        current_message = HumanMessage(content="Use $unknown and $../outside.")

        messages = ContextBuilder(self._workspace).build_request_messages((), current_message)

        self.assertNotIn("[Active Skills for this turn]", messages[0].content)
        self.assertEqual(messages[-1], current_message)

    def test_explicit_skill_contents_are_never_executed(self) -> None:
        self._write_skill(
            "static",
            "---\nname: static\n---\npython -c \\\"raise RuntimeError('must not run')\\\"\n",
        )

        messages = ContextBuilder(self._workspace).build_request_messages(
            (),
            HumanMessage(content="Use $static."),
        )

        self.assertIn("raise RuntimeError('must not run')", messages[0].content)

    def test_explicit_skill_loading_failure_does_not_break_request_construction(self) -> None:
        broken_path = self._workspace / "skills" / "broken" / "SKILL.md"
        broken_path.parent.mkdir(parents=True)
        broken_path.write_bytes(b"\xff")
        self._write_skill(
            "valid",
            "---\nname: valid\n---\nValid explicit instructions.\n",
        )

        messages = ContextBuilder(self._workspace).build_request_messages(
            (),
            HumanMessage(content="Use $valid and $broken."),
        )

        self.assertIn("Valid explicit instructions.", messages[0].content)
        self.assertNotIn("broken", messages[0].content)

    def test_long_term_memory_does_not_modify_session_messages(self) -> None:
        session = Session.create("memory-test").with_messages(
            (HumanMessage(content="Previous question."),)
        )
        original_messages = session.messages

        messages = ContextBuilder(self._workspace).build_request_messages(
            session.messages,
            HumanMessage(content="Current question."),
            memory="Persistent user preference.",
        )

        self.assertEqual(session.messages, original_messages)
        self.assertIn("Persistent user preference.", messages[0].content)

    def test_reloads_files_without_modifying_the_workspace(self) -> None:
        path = self._write("SOUL.md", "First style.")
        before = path.read_bytes(), path.stat().st_mtime_ns
        builder = ContextBuilder(self._workspace)

        first_prompt = builder.build_system_prompt()
        after_first_build = path.read_bytes(), path.stat().st_mtime_ns
        path.write_text("Second style.", encoding="utf-8")
        second_prompt = builder.build_system_prompt()

        self.assertIn("First style.", first_prompt)
        self.assertIn("Second style.", second_prompt)
        self.assertNotIn("First style.", second_prompt)
        self.assertEqual(after_first_build, before)
        self.assertFalse((self._workspace / "sessions").exists())

    def test_assembles_every_unsummarized_message_in_order(self) -> None:
        call = ToolCallRequest(id="call", name="read", arguments={})
        question = HumanMessage("Repeat this.")
        history = (
            HumanMessage("Earlier request."), AIMessage("", tool_calls=(call,)),
            ToolMessage("x" * 600_000, tool_call_id=call.id), AIMessage("Done."), question,
        )
        request = ContextBuilder(self._workspace).build_request_messages(history, question)
        self.assertIsInstance(request[0], SystemMessage)
        self.assertEqual(request[1:], (*history, question))

    def test_places_saved_summary_before_recent_messages(self) -> None:
        builder = ContextBuilder(self._workspace)
        history = (HumanMessage("Use local caching instead."), AIMessage("Understood."))
        current = HumanMessage("Write the design.")
        system, summary, *recent = builder.build_request_messages(
            history, current, summary="Originally selected Redis.",
        )
        self.assertEqual(system, SystemMessage(builder.build_system_prompt()))
        self.assertIsInstance(summary, HumanMessage)
        self.assertIn("Originally selected Redis.", summary.content)
        self.assertEqual(recent, [*history, current])

    def test_token_estimate_grows_with_text_and_counts_tool_call_arguments(self) -> None:
        short_message = HumanMessage(content="short")
        long_message = HumanMessage(content="long " * 40)
        without_tool_call = AIMessage(content="Calling a tool.")
        with_tool_call = AIMessage(
            content="Calling a tool.",
            tool_calls=(
                ToolCallRequest(
                    id="call-1",
                    name="get_weather",
                    arguments={"city": "Shanghai", "units": "celsius"},
                ),
            ),
        )

        self.assertGreater(
            estimate_message_tokens(long_message),
            estimate_message_tokens(short_message),
        )
        self.assertGreater(
            estimate_message_tokens(with_tool_call),
            estimate_message_tokens(without_tool_call),
        )
        self.assertGreater(
            estimate_message_tokens(HumanMessage(content="你好你好")),
            estimate_message_tokens(HumanMessage(content="abcd")),
        )
        self.assertGreater(estimate_message_tokens(HumanMessage(content="")), 0)

    def _write(self, filename: str, content: str) -> Path:
        path = self._workspace / filename
        path.write_text(content, encoding="utf-8")
        return path

    def _write_skill(self, directory_name: str, content: str) -> Path:
        path = self._workspace / "skills" / directory_name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path
