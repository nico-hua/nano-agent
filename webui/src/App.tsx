import {
  type FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  createSessionId,
  deleteSession,
  fetchSessionHistory,
  fetchSessionSummaries,
  SessionApiError,
} from "./api/sessions";
import { getSlashCommandSuggestions } from "./commands";
import { CommandSuggestionPanel } from "./components/CommandSuggestionPanel";
import { LanguageToggle } from "./components/LanguageToggle";
import { MessageContent } from "./components/MessageContent";
import { SessionDeleteButton } from "./components/SessionDeleteButton";
import { isNearConversationBottom } from "./conversationScroll";
import { visibleChatMessages } from "./hooks/chatState";
import {
  UI_COPY,
  formatMessageCount,
  formatUpdatedAt,
  rawError,
  readStoredLanguage,
  resolveDisplayError,
  storeLanguage,
  uiError,
  type DisplayError,
  type Language,
} from "./i18n";
import {
  type AuthenticationStatus,
  type ConnectionStatus,
  useNanobotWebSocket,
} from "./hooks/useNanobotWebSocket";
import type { SessionInfo } from "./types/protocol";
import "./App.css";

const CHAT_ID = "webui-default-chat";
const API_BASE_URL =
  import.meta.env.VITE_NANOBOT_API_URL ?? "http://127.0.0.1:8000";
const AUTH_TOKEN = import.meta.env.VITE_NANOBOT_AUTH_TOKEN;

function App() {
  const [language, setLanguage] = useState<Language>(() =>
    readStoredLanguage(window.localStorage),
  );
  const copy = UI_COPY[language];
  const [draft, setDraft] = useState("");
  const [sessionId, setSessionId] = useState(() => createSessionId());
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [isLoadingSessions, setIsLoadingSessions] = useState(true);
  const [isLoadingHistory, setIsLoadingHistory] = useState(false);
  const [sessionError, setSessionError] = useState<DisplayError | null>(null);
  const [sessionPendingDeletion, setSessionPendingDeletion] =
    useState<SessionInfo | null>(null);
  const [isDeletingSession, setIsDeletingSession] = useState(false);
  const [deleteSessionError, setDeleteSessionError] =
    useState<DisplayError | null>(null);
  const conversationRef = useRef<HTMLDivElement>(null);
  const draftInputRef = useRef<HTMLTextAreaElement>(null);
  const shouldFollowLatestRef = useRef(true);
  const historyRequestRef = useRef(0);
  const wasSendingRef = useRef(false);
  const lastConnectionVersionRef = useRef(0);
  const activeSessionIdRef = useRef(sessionId);
  activeSessionIdRef.current = sessionId;
  const {
    connectionStatus,
    authenticationStatus,
    connectionVersion,
    error,
    isSending,
    isStopping,
    messages,
    sendMessage,
    stopGeneration,
    reconnect,
    replaceMessages,
  } = useNanobotWebSocket({
    url: import.meta.env.VITE_NANOBOT_WEBSOCKET_URL,
    chatId: CHAT_ID,
    sessionId,
    authToken: AUTH_TOKEN,
  });

  useEffect(() => {
    document.documentElement.lang = language;
    storeLanguage(window.localStorage, language);
  }, [language]);

  const refreshSessions = useCallback(async () => {
    try {
      const summaries = await fetchSessionSummaries(API_BASE_URL, AUTH_TOKEN);
      setSessions(summaries);
      setSessionError(null);
    } catch (caughtError) {
      setSessionError(displayError(caughtError));
    } finally {
      setIsLoadingSessions(false);
    }
  }, []);

  useEffect(() => {
    void refreshSessions();
  }, [refreshSessions]);

  const loadSessionHistory = useCallback((targetSessionId: string) => {
    const requestId = historyRequestRef.current + 1;
    historyRequestRef.current = requestId;
    setIsLoadingHistory(true);

    void fetchSessionHistory(API_BASE_URL, targetSessionId, AUTH_TOKEN)
      .then((history) => {
        if (historyRequestRef.current !== requestId) {
          return;
        }
        replaceMessages(history.messages);
        shouldFollowLatestRef.current = true;
        setSessionError(null);
      })
      .catch((caughtError: unknown) => {
        if (historyRequestRef.current !== requestId) {
          return;
        }
        if (caughtError instanceof SessionApiError && caughtError.status === 404) {
          // A browser-created session does not exist on disk until its first turn.
          replaceMessages([]);
          return;
        }
        replaceMessages([]);
        setSessionError(displayError(caughtError));
      })
      .finally(() => {
        if (historyRequestRef.current === requestId) {
          setIsLoadingHistory(false);
        }
      });
  }, [replaceMessages]);

  useEffect(() => {
    loadSessionHistory(sessionId);
  }, [loadSessionHistory, sessionId]);

  useEffect(() => {
    if (connectionStatus !== "connected" || connectionVersion === 0) {
      return;
    }

    const previousVersion = lastConnectionVersionRef.current;
    if (previousVersion === connectionVersion) {
      return;
    }
    lastConnectionVersionRef.current = connectionVersion;
    if (previousVersion !== 0) {
      // A new socket cannot replay deltas. Reload only persisted history once
      // the replacement connection has opened.
      loadSessionHistory(activeSessionIdRef.current);
    }
  }, [connectionStatus, connectionVersion, loadSessionHistory]);

  useEffect(() => {
    if (wasSendingRef.current && !isSending) {
      void refreshSessions();
    }
    wasSendingRef.current = isSending;
  }, [isSending, refreshSessions]);

  const authenticationReady =
    authenticationStatus === "not_required" ||
    authenticationStatus === "authenticated";
  const canSend = Boolean(
    connectionStatus === "connected" &&
      authenticationReady &&
      !isSending &&
      !isLoadingHistory &&
      draft.trim(),
  );
  const isComposerDisabled =
    isSending ||
    isLoadingHistory ||
    connectionStatus !== "connected" ||
    !authenticationReady;
  const commandSuggestions = getSlashCommandSuggestions(draft, copy.commands);
  const displayedMessages = visibleChatMessages(messages);

  useEffect(() => {
    const conversation = conversationRef.current;
    if (conversation === null || !shouldFollowLatestRef.current) {
      return;
    }
    conversation.scrollTop = conversation.scrollHeight;
  }, [messages]);

  function handleConversationScroll() {
    const conversation = conversationRef.current;
    if (conversation !== null) {
      shouldFollowLatestRef.current = isNearConversationBottom(conversation);
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (sendMessage(draft)) {
      // A sent message intentionally returns the reader to the active turn.
      shouldFollowLatestRef.current = true;
      setDraft("");
    }
  }

  function handleStop() {
    if (stopGeneration()) {
      shouldFollowLatestRef.current = true;
    }
  }

  function handleReconnect() {
    reconnect();
  }

  function selectSession(nextSessionId: string) {
    if (nextSessionId === sessionId) {
      return;
    }
    shouldFollowLatestRef.current = true;
    replaceMessages([]);
    setSessionId(nextSessionId);
  }

  function createNewSession() {
    shouldFollowLatestRef.current = true;
    replaceMessages([]);
    setSessionError(null);
    setSessionId(createSessionId());
  }

  function requestSessionDeletion(session: SessionInfo) {
    setDeleteSessionError(null);
    setSessionPendingDeletion(session);
  }

  function cancelSessionDeletion() {
    if (isDeletingSession) {
      return;
    }
    setDeleteSessionError(null);
    setSessionPendingDeletion(null);
  }

  async function confirmSessionDeletion() {
    const target = sessionPendingDeletion;
    if (target === null || isDeletingSession) {
      return;
    }
    setIsDeletingSession(true);
    setDeleteSessionError(null);
    try {
      await deleteSession(API_BASE_URL, target.sessionId, AUTH_TOKEN);
      historyRequestRef.current += 1;
      shouldFollowLatestRef.current = true;
      replaceMessages([]);
      setDraft("");
      setSessions((current) =>
        current.filter((session) => session.sessionId !== target.sessionId),
      );
      setSessionError(null);
      setSessionPendingDeletion(null);
      setSessionId(createSessionId());
      void refreshSessions();
    } catch (caughtError) {
      setDeleteSessionError(displayError(caughtError));
    } finally {
      setIsDeletingSession(false);
    }
  }

  function handleCommandSelection(insertText: string) {
    setDraft(insertText);
    draftInputRef.current?.focus();
  }

  const connectionLabel =
    connectionStatus === "connected"
      ? authenticationLabel(authenticationStatus, copy)
      : connectionStatusLabel(connectionStatus, copy);
  const connectionClass =
    connectionStatus === "connected"
      ? authenticationStatus === "authenticated"
        ? "authenticated"
        : authenticationStatus
      : connectionStatus;

  return (
    <main className="app-shell" aria-label={copy.app.ariaLabel}>
      <header className="app-header">
        <img
          className="app-logo"
          src="/nanobot-logo.png"
          alt={copy.app.logoAlt}
        />
        <h1>Nanobot Web UI</h1>
        <LanguageToggle
          language={language}
          copy={copy.language}
          onChange={setLanguage}
        />
      </header>

      <div className="app-workspace">
        <aside
          className="session-sidebar"
          aria-label={copy.sessions.sectionLabel}
        >
          <div className="session-sidebar__header">
            <div>
              <h2>{copy.sessions.title}</h2>
            </div>
            <button type="button" onClick={createNewSession}>
              {copy.sessions.newSession}
            </button>
          </div>

          {sessionError !== null ? (
            <p className="session-error" role="alert">
              {resolveDisplayError(copy, sessionError)}
            </p>
          ) : null}

          <div className="session-list" aria-live="polite">
            {isLoadingSessions ? <p>{copy.sessions.loading}</p> : null}
            {!isLoadingSessions && sessions.length === 0 ? (
              <p>{copy.sessions.empty}</p>
            ) : null}
            <ol>
              {sessions.map((session) => (
                <li key={session.sessionId}>
                  <div className="session-row">
                    <button
                      type="button"
                      className={
                        session.sessionId === sessionId
                          ? "session-item session-item--active"
                          : "session-item"
                      }
                      onClick={() => selectSession(session.sessionId)}
                      aria-current={
                        session.sessionId === sessionId ? "page" : undefined
                      }
                    >
                      <span className="session-item__id">{session.sessionId}</span>
                      <span className="session-item__preview">
                        {session.preview || copy.sessions.noVisibleMessages}
                      </span>
                      <span className="session-item__meta">
                        {formatMessageCount(copy, session.messageCount)}
                        {" · "}
                        {formatUpdatedAt(language, session.updatedAt, copy)}
                      </span>
                    </button>
                    <SessionDeleteButton
                      ariaLabel={copy.sessions.deleteLabel(session.sessionId)}
                      title={copy.sessions.deleteTitle}
                      onDelete={() => requestSessionDeletion(session)}
                    />
                  </div>
                </li>
              ))}
            </ol>
          </div>
        </aside>

        <div className="chat-workspace">
          <section className="chat-panel" aria-labelledby="conversation-title">
            <div className="chat-panel__header">
              <h2 id="conversation-title">{copy.conversation.title}</h2>
              <div className="chat-panel__connection">
                <span className={`status-badge status-badge--${connectionClass}`}>
                  {connectionLabel}
                </span>
                {connectionStatus === "error" ||
                connectionStatus === "disconnected" ? (
                  <button
                    type="button"
                    className="connection-retry"
                    onClick={handleReconnect}
                  >
                    {copy.connection.reconnect}
                  </button>
                ) : null}
              </div>
            </div>

            <div className="chat-panel__body">
              {error !== null ? (
                <p className="connection-error" role="alert">
                  {resolveDisplayError(copy, error)}
                </p>
              ) : null}

              <div
                ref={conversationRef}
                className="conversation-scroll"
                onScroll={handleConversationScroll}
              >
                {isLoadingHistory && displayedMessages.length === 0 ? (
                  <div className="chat-empty-state">
                    <p>{copy.conversation.loading}</p>
                  </div>
                ) : null}
                {!isLoadingHistory && displayedMessages.length === 0 ? (
                  <div className="chat-empty-state">
                    <p>{copy.conversation.empty}</p>
                    <span>
                      {connectionStatus === "connected"
                        ? copy.conversation.start
                        : copy.conversation.waiting}
                    </span>
                  </div>
                ) : null}
                {displayedMessages.length > 0 ? (
                  <ol className="message-list" aria-live="polite">
                    {displayedMessages.map((message) => (
                      <li
                        key={message.id}
                        className={`message message--${message.role}`}
                      >
                        <MessageContent {...message} copy={copy.messages} />
                      </li>
                    ))}
                  </ol>
                ) : null}
              </div>
            </div>
          </section>

          <form
            className="composer"
            aria-label={copy.composer.label}
            onSubmit={handleSubmit}
          >
            {commandSuggestions.length > 0 ? (
              <CommandSuggestionPanel
                suggestions={commandSuggestions}
                ariaLabel={copy.commands.panelLabel}
                onSelect={handleCommandSelection}
              />
            ) : null}
            <div className="composer__controls">
              <textarea
                id="message"
                name="message"
                aria-label={copy.composer.inputLabel}
                ref={draftInputRef}
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                placeholder={copy.composer.placeholder}
                rows={1}
                disabled={isComposerDisabled}
              />
              <div className="composer__actions">
                {isSending ? (
                  <button
                    type="button"
                    className="composer__stop"
                    onClick={handleStop}
                    disabled={isStopping}
                  >
                    {isStopping ? copy.composer.stopping : copy.composer.stop}
                  </button>
                ) : null}
                <button type="submit" disabled={!canSend}>
                  {isSending ? copy.composer.sending : copy.composer.send}
                </button>
              </div>
            </div>
          </form>
        </div>
      </div>

      {sessionPendingDeletion !== null ? (
        <div className="modal-backdrop">
          <section
            className="confirmation-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="delete-session-title"
            aria-describedby="delete-session-description"
          >
            <h2 id="delete-session-title">{copy.deletion.title}</h2>
            <p id="delete-session-description">
              {copy.deletion.description}
            </p>
            <p className="confirmation-modal__session">
              {sessionPendingDeletion.sessionId}
            </p>
            {deleteSessionError !== null ? (
              <p className="confirmation-modal__error" role="alert">
                {resolveDisplayError(copy, deleteSessionError)}
              </p>
            ) : null}
            <div className="confirmation-modal__actions">
              <button
                type="button"
                onClick={cancelSessionDeletion}
                disabled={isDeletingSession}
                autoFocus
              >
                {copy.deletion.cancel}
              </button>
              <button
                type="button"
                className="confirmation-modal__delete"
                onClick={() => void confirmSessionDeletion()}
                disabled={isDeletingSession}
              >
                {isDeletingSession
                  ? copy.deletion.deleting
                  : copy.deletion.deleteSession}
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </main>
  );
}

function displayError(caughtError: unknown): DisplayError {
  if (caughtError instanceof SessionApiError) {
    return caughtError.uiErrorCode
      ? uiError(caughtError.uiErrorCode)
      : rawError(caughtError.message);
  }
  return caughtError instanceof Error
    ? rawError(caughtError.message)
    : uiError("session_load_failed");
}

function connectionStatusLabel(
  status: ConnectionStatus,
  copy: (typeof UI_COPY)[Language],
): string {
  return copy.connection[status];
}

function authenticationLabel(
  status: AuthenticationStatus,
  copy: (typeof UI_COPY)[Language],
): string {
  const labels: Record<AuthenticationStatus, string> = {
    checking: copy.connection.authenticating,
    not_required: copy.connection.connected,
    authenticating: copy.connection.authenticating,
    authenticated: copy.connection.authenticated,
    failed: copy.connection.authenticationFailed,
  };
  return labels[status];
}

export default App;
