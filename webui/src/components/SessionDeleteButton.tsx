type SessionDeleteButtonProps = {
  sessionId: string;
  onDelete: () => void;
};

export function SessionDeleteButton({
  sessionId,
  onDelete,
}: SessionDeleteButtonProps) {
  return (
    <button
      type="button"
      className="session-delete"
      onClick={onDelete}
      aria-label={`Delete session ${sessionId}`}
      title="Delete session"
    >
      <svg
        viewBox="0 0 24 24"
        aria-hidden="true"
        focusable="false"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M4 7h16" />
        <path d="M9 7V4h6v3" />
        <path d="m6 7 1 13h10l1-13" />
        <path d="M10 11v5" />
        <path d="M14 11v5" />
      </svg>
    </button>
  );
}
