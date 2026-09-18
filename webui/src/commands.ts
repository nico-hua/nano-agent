import type { CommandCopyKey, UiCopy } from "./i18n.js";

/** A front-end-only catalogue of the slash commands handled by CommandRouter. */
export type SlashCommandSuggestion = {
  usage: string;
  insertText: string;
  description: string;
};

const COMMANDS: readonly { key: CommandCopyKey; insertText: string }[] = [
  { key: "new", insertText: "/new" },
  { key: "stop", insertText: "/stop" },
  { key: "help", insertText: "/help" },
  { key: "goal", insertText: "/goal " },
  { key: "goalStatus", insertText: "/goal status" },
  { key: "goalStop", insertText: "/goal stop" },
  { key: "compact", insertText: "/compact" },
  { key: "memory", insertText: "/memory" },
  { key: "subagents", insertText: "/subagents" },
  { key: "subagentsStatus", insertText: "/subagents status " },
  { key: "subagentsCancel", insertText: "/subagents cancel " },
];

/** Return matching suggestions only while the user is composing a slash command. */
export function getSlashCommandSuggestions(
  input: string,
  copy: UiCopy["commands"],
): readonly SlashCommandSuggestion[] {
  const query = input.trimStart().toLocaleLowerCase();
  if (!query.startsWith("/")) {
    return [];
  }
  return COMMANDS.map(({ key, insertText }) => ({
    ...copy.entries[key],
    insertText,
  })).filter((suggestion) =>
    suggestion.usage.toLocaleLowerCase().startsWith(query),
  );
}
