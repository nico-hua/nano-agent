import type { SlashCommandSuggestion } from "../commands";

type CommandSuggestionPanelProps = {
  suggestions: readonly SlashCommandSuggestion[];
  ariaLabel: string;
  onSelect: (insertText: string) => void;
};

/** Present command choices without taking ownership of command execution. */
export function CommandSuggestionPanel({
  suggestions,
  ariaLabel,
  onSelect,
}: CommandSuggestionPanelProps) {
  return (
    <section className="command-suggestion-panel" aria-label={ariaLabel}>
      <ul>
        {suggestions.map((suggestion) => (
          <li key={suggestion.usage}>
            <button
              type="button"
              className="command-suggestion-panel__item"
              onClick={() => onSelect(suggestion.insertText)}
            >
              <code>{suggestion.usage}</code>
              <span>{suggestion.description}</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
