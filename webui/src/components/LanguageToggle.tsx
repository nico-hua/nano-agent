import type { Language, UiCopy } from "../i18n.js";

type LanguageToggleProps = {
  language: Language;
  copy: UiCopy["language"];
  onChange: (language: Language) => void;
};

export function LanguageToggle({
  language,
  copy,
  onChange,
}: LanguageToggleProps) {
  return (
    <div className="language-toggle" role="group" aria-label={copy.label}>
      <button
        type="button"
        aria-pressed={language === "zh-CN"}
        onClick={() => onChange("zh-CN")}
      >
        {copy.chinese}
      </button>
      <button
        type="button"
        aria-pressed={language === "en"}
        onClick={() => onChange("en")}
      >
        {copy.english}
      </button>
    </div>
  );
}
