export type Language = "zh-CN" | "en";

export type UiErrorCode =
  | "connection_closed"
  | "connection_interrupted"
  | "reconnect_failed"
  | "missing_websocket_url"
  | "invalid_server_event"
  | "auth_token_required"
  | "auth_incomplete"
  | "auth_failed"
  | "not_connected_send"
  | "auth_not_ready"
  | "not_connected_stop"
  | "server_processing_failed"
  | "session_api_unreachable"
  | "session_api_invalid_response"
  | "session_data_invalid"
  | "session_load_failed";

export type DisplayError =
  | { kind: "ui"; code: UiErrorCode }
  | { kind: "raw"; message: string };

export type CommandCopyKey =
  | "new"
  | "stop"
  | "help"
  | "goal"
  | "goalStatus"
  | "goalStop"
  | "compact"
  | "memory"
  | "subagents"
  | "subagentsStatus"
  | "subagentsCancel";

type CommandCopy = {
  usage: string;
  description: string;
};

export type UiCopy = {
  app: {
    ariaLabel: string;
    logoAlt: string;
  };
  language: {
    label: string;
    chinese: string;
    english: string;
  };
  sessions: {
    sectionLabel: string;
    title: string;
    newSession: string;
    loading: string;
    empty: string;
    noVisibleMessages: string;
    recentlyUpdated: string;
    messageCount: (count: number) => string;
    deleteLabel: (sessionId: string) => string;
    deleteTitle: string;
  };
  conversation: {
    title: string;
    loading: string;
    empty: string;
    start: string;
    waiting: string;
  };
  connection: {
    connecting: string;
    connected: string;
    reconnecting: string;
    disconnected: string;
    error: string;
    authenticating: string;
    authenticated: string;
    authenticationFailed: string;
    reconnect: string;
  };
  composer: {
    label: string;
    inputLabel: string;
    placeholder: string;
    stop: string;
    stopping: string;
    send: string;
    sending: string;
  };
  deletion: {
    title: string;
    description: string;
    cancel: string;
    deleteSession: string;
    deleting: string;
  };
  messages: {
    toolCallsLabel: string;
    calledTool: (name: string) => string;
    thinking: string;
  };
  commands: {
    panelLabel: string;
    entries: Record<CommandCopyKey, CommandCopy>;
  };
  errors: Record<UiErrorCode, string>;
};

export const DEFAULT_LANGUAGE: Language = "zh-CN";
export const LANGUAGE_STORAGE_KEY = "nanobot.webui.language";

export const UI_COPY: Record<Language, UiCopy> = {
  "zh-CN": {
    app: {
      ariaLabel: "Nanobot 聊天",
      logoAlt: "Nanobot 标志",
    },
    language: {
      label: "界面语言",
      chinese: "中文",
      english: "EN",
    },
    sessions: {
      sectionLabel: "会话",
      title: "会话列表",
      newSession: "新建会话",
      loading: "正在加载会话……",
      empty: "还没有保存的会话。",
      noVisibleMessages: "没有可显示的消息",
      recentlyUpdated: "最近更新",
      messageCount: (count) => `${count} 条消息`,
      deleteLabel: (sessionId) => `删除会话 ${sessionId}`,
      deleteTitle: "删除会话",
    },
    conversation: {
      title: "对话",
      loading: "正在加载对话……",
      empty: "还没有消息。",
      start: "发送消息以开始对话。",
      waiting: "正在等待连接本地 Nanobot 服务。",
    },
    connection: {
      connecting: "连接中",
      connected: "已连接",
      reconnecting: "正在重连",
      disconnected: "已断开",
      error: "连接错误",
      authenticating: "认证中",
      authenticated: "已认证",
      authenticationFailed: "认证失败",
      reconnect: "重新连接",
    },
    composer: {
      label: "消息输入区",
      inputLabel: "消息",
      placeholder: "向 Nanobot 提问……",
      stop: "停止",
      stopping: "正在停止……",
      send: "发送",
      sending: "发送中……",
    },
    deletion: {
      title: "删除这个会话？",
      description:
        "该会话将被永久删除且无法恢复。仍在运行的相关任务会先被停止。",
      cancel: "取消",
      deleteSession: "删除会话",
      deleting: "正在删除……",
    },
    messages: {
      toolCallsLabel: "工具调用",
      calledTool: (name) => `调用 ${name}`,
      thinking: "思考中……",
    },
    commands: {
      panelLabel: "斜杠命令",
      entries: {
        new: { usage: "/new", description: "清空当前会话历史，不创建新的 session。" },
        stop: { usage: "/stop", description: "停止当前会话正在执行的请求。" },
        help: { usage: "/help", description: "显示后端当前已注册的命令。" },
        goal: {
          usage: "/goal <目标描述>",
          description: "创建并开始执行当前会话的持续目标。",
        },
        goalStatus: {
          usage: "/goal status",
          description: "查看当前会话的目标状态。",
        },
        goalStop: {
          usage: "/goal stop",
          description: "停止当前会话正在执行的目标。",
        },
        compact: {
          usage: "/compact",
          description: "将当前会话较早的完整历史整理为摘要。",
        },
        memory: {
          usage: "/memory",
          description: "查看当前 workspace 的长期记忆。",
        },
        subagents: {
          usage: "/subagents",
          description: "列出当前会话的后台子 Agent 任务。",
        },
        subagentsStatus: {
          usage: "/subagents status <task_id>",
          description: "查看指定后台子 Agent 任务的状态。",
        },
        subagentsCancel: {
          usage: "/subagents cancel <task_id>",
          description: "取消指定后台子 Agent 任务。",
        },
      },
    },
    errors: {
      connection_closed: "Nanobot WebSocket 连接已关闭。",
      connection_interrupted: "连接已中断，正在重新连接并恢复已保存的历史记录。",
      reconnect_failed: "无法重新连接到 Nanobot WebSocket 服务。",
      missing_websocket_url: "未配置 VITE_NANOBOT_WEBSOCKET_URL。",
      invalid_server_event: "收到了来自 Nanobot 的无效消息。",
      auth_token_required: "Nanobot 认证需要配置浏览器令牌。",
      auth_incomplete: "无法完成 Nanobot 认证。",
      auth_failed: "Nanobot 认证失败。",
      not_connected_send: "Nanobot 尚未连接，请等待连接完成后再发送。",
      auth_not_ready: "Nanobot 认证尚未就绪，请等待连接完成后再发送。",
      not_connected_stop: "Nanobot 尚未连接，无法停止当前生成。",
      server_processing_failed: "Nanobot 无法处理该消息。",
      session_api_unreachable: "无法连接本地 Nanobot 会话 API。",
      session_api_invalid_response: "Nanobot 返回了无效的会话 API 响应。",
      session_data_invalid: "Nanobot 返回了无效的会话数据。",
      session_load_failed: "无法加载保存的会话。",
    },
  },
  en: {
    app: {
      ariaLabel: "Nanobot chat",
      logoAlt: "Nanobot logo",
    },
    language: {
      label: "Interface language",
      chinese: "中文",
      english: "EN",
    },
    sessions: {
      sectionLabel: "Sessions",
      title: "Sessions",
      newSession: "New session",
      loading: "Loading sessions...",
      empty: "No saved sessions yet.",
      noVisibleMessages: "No visible messages",
      recentlyUpdated: "Recently updated",
      messageCount: (count) => `${count} message${count === 1 ? "" : "s"}`,
      deleteLabel: (sessionId) => `Delete session ${sessionId}`,
      deleteTitle: "Delete session",
    },
    conversation: {
      title: "Conversation",
      loading: "Loading conversation...",
      empty: "No messages yet.",
      start: "Send a message to start a conversation.",
      waiting: "Waiting for the local Nanobot connection.",
    },
    connection: {
      connecting: "Connecting",
      connected: "Connected",
      reconnecting: "Reconnecting",
      disconnected: "Disconnected",
      error: "Connection error",
      authenticating: "Authenticating",
      authenticated: "Authenticated",
      authenticationFailed: "Authentication failed",
      reconnect: "Reconnect",
    },
    composer: {
      label: "Message composer",
      inputLabel: "Message",
      placeholder: "Ask Nanobot anything...",
      stop: "Stop",
      stopping: "Stopping...",
      send: "Send",
      sending: "Sending...",
    },
    deletion: {
      title: "Delete this session?",
      description:
        "This session will be permanently deleted and cannot be recovered. Any work still running for it will be stopped first.",
      cancel: "Cancel",
      deleteSession: "Delete session",
      deleting: "Deleting...",
    },
    messages: {
      toolCallsLabel: "Tool calls",
      calledTool: (name) => `Called ${name}`,
      thinking: "Thinking...",
    },
    commands: {
      panelLabel: "Slash commands",
      entries: {
        new: { usage: "/new", description: "Clear this conversation without creating a new session." },
        stop: { usage: "/stop", description: "Stop the request currently running in this conversation." },
        help: { usage: "/help", description: "Show the commands currently registered by the backend." },
        goal: {
          usage: "/goal <objective>",
          description: "Create and start a persistent goal for this conversation.",
        },
        goalStatus: {
          usage: "/goal status",
          description: "Show the current goal status for this conversation.",
        },
        goalStop: {
          usage: "/goal stop",
          description: "Stop the goal currently running in this conversation.",
        },
        compact: {
          usage: "/compact",
          description: "Summarize the older complete history of this conversation.",
        },
        memory: {
          usage: "/memory",
          description: "Show long-term memory for the current workspace.",
        },
        subagents: {
          usage: "/subagents",
          description: "List background subagent tasks for this conversation.",
        },
        subagentsStatus: {
          usage: "/subagents status <task_id>",
          description: "Show the status of a background subagent task.",
        },
        subagentsCancel: {
          usage: "/subagents cancel <task_id>",
          description: "Cancel a background subagent task.",
        },
      },
    },
    errors: {
      connection_closed: "The Nanobot WebSocket connection was closed.",
      connection_interrupted: "Connection interrupted. Reconnecting and restoring saved history.",
      reconnect_failed: "Could not reconnect to the Nanobot WebSocket service.",
      missing_websocket_url: "VITE_NANOBOT_WEBSOCKET_URL is not configured.",
      invalid_server_event: "Received an invalid message from Nanobot.",
      auth_token_required: "Nanobot authentication requires a configured browser token.",
      auth_incomplete: "Nanobot authentication could not be completed.",
      auth_failed: "Nanobot authentication failed.",
      not_connected_send: "Nanobot is not connected. Wait for the connection before sending.",
      auth_not_ready: "Nanobot authentication is not ready. Wait for the connection before sending.",
      not_connected_stop: "Nanobot is not connected. The current generation could not be stopped.",
      server_processing_failed: "Nanobot could not process the message.",
      session_api_unreachable: "Could not reach the local Nanobot session API.",
      session_api_invalid_response: "Nanobot returned an invalid session API response.",
      session_data_invalid: "Nanobot returned invalid session data.",
      session_load_failed: "Could not load saved sessions.",
    },
  },
};

type ReadableStorage = Pick<Storage, "getItem">;
type WritableStorage = Pick<Storage, "setItem">;

export function readStoredLanguage(
  storage: ReadableStorage | undefined,
): Language {
  try {
    const stored = storage?.getItem(LANGUAGE_STORAGE_KEY);
    return stored === "zh-CN" || stored === "en" ? stored : DEFAULT_LANGUAGE;
  } catch {
    return DEFAULT_LANGUAGE;
  }
}

export function storeLanguage(
  storage: WritableStorage | undefined,
  language: Language,
): boolean {
  try {
    storage?.setItem(LANGUAGE_STORAGE_KEY, language);
    return storage !== undefined;
  } catch {
    return false;
  }
}

export function uiError(code: UiErrorCode): DisplayError {
  return { kind: "ui", code };
}

export function rawError(message: string): DisplayError {
  return { kind: "raw", message };
}

export function resolveDisplayError(copy: UiCopy, error: DisplayError): string {
  return error.kind === "ui" ? copy.errors[error.code] : error.message;
}

export function formatMessageCount(copy: UiCopy, count: number): string {
  return copy.sessions.messageCount(count);
}

export function formatUpdatedAt(
  language: Language,
  value: string,
  copy: UiCopy,
): string {
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) {
    return copy.sessions.recentlyUpdated;
  }
  return new Intl.DateTimeFormat(language, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(timestamp);
}
