export type SetupState = {
  enabled: boolean;
  ready: boolean;
  workspace_slug: string;
  provider: string;
  blockers: Array<{ code: string; message: string }>;
};

export type Channel = {
  id: number;
  identifier: string;
  title: string;
  active?: boolean;
};

export type Conversation = {
  id: string;
  workspace_id: string;
  channel_id: number;
  channel_identifier: string;
  channel_title: string;
  title: string;
  summary: string;
  active_draft_id?: string | null;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
};

export type DraftClaim = { claim: string; source_ids: string[] };

export type Draft = {
  id: string;
  workspace_id: string;
  conversation_id: string;
  channel_id: number;
  story_cluster_id?: string | null;
  analysis_id?: string | null;
  working_title: string;
  body: string;
  status: string;
  source_ids: string[];
  claim_support: DraftClaim[];
  assumptions: string[];
  warnings: string[];
  channel_evidence: Array<Record<string, unknown>>;
  web_evidence: Array<Record<string, unknown>>;
  confidence: "high" | "medium" | "low" | string;
  creative: boolean;
  provider: string;
  model: string;
  prompt_version: string;
  revision: number;
  current_version: number;
  current_version_origin: string;
  character_count: number;
  over_limit: boolean;
  warning_threshold: boolean;
  copied_at: string | null;
  created_at: string | null;
  updated_at: string | null;
};

export type DraftVersion = {
  id: number;
  draft_id: string;
  version: number;
  body: string;
  origin: "generated" | "regenerated" | "user_edit" | string;
  instruction: string;
  character_count: number;
  created_at: string | null;
};

export type PersistedMessage = {
  id: number;
  role: "user" | "assistant" | "system_summary";
  content: string;
  metadata: Record<string, unknown>;
  created_at: string;
};

export type RunSummary = {
  id: string;
  conversation_id: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled" | "interrupted";
  stage: string;
  provider: string;
  requested_model: string;
  actual_model: string | null;
  usage: RunUsage;
  error_code: string | null;
  error_message: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
};

export type RunUsage = {
  requests: number;
  tool_calls: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
  estimated_cost_usd?: number;
  latency_ms?: number;
  details?: Record<string, number>;
};

export type RunActivity = {
  event_count: number;
  tool_call_count: number;
  result_count: number;
  source_count: number;
  story_count: number;
  cache_hit_count: number;
  degraded: boolean;
  references: Record<string, string[]>;
};

export type RunDetails = {
  provider: string;
  requested_model: string;
  actual_model: string | null;
  prompt_version: string;
  status: RunSummary["status"];
  stage: string;
  duration_ms: number | null;
  usage: RunUsage;
  activity: RunActivity;
  error: { code: string; message: string; retryable: boolean } | null;
};

export type RunEvent = {
  id: number;
  sequence: number;
  event_type: string;
  safe_payload: Record<string, unknown>;
  created_at: string | null;
};

export type Bootstrap = {
  setup: SetupState;
  workspace: { id: string; slug: string; role: string };
  user: { id: string | null };
  provider: { name: string; model: string; configured: boolean };
  channels: Channel[];
  selected_channel_id: number | null;
  conversations: Conversation[];
  current_conversation: Conversation | null;
  draft: Draft | null;
  active_run: RunSummary | null;
  consent: {
    provider: string;
    granted: boolean;
    required: boolean;
    configuration_fingerprint?: string;
    disclosure?: { title: string; message: string } | null;
  };
  profile_status: "no_channel" | "needs_consent" | "not_analyzed" | "low_confidence" | "ready";
  profile: {
    topics: Array<{ name: string; claim?: string }>;
    confidence: string;
    version: number;
    style_profile?: Record<string, unknown>;
    editorial_rules?: Record<string, unknown>;
  } | null;
};

export type ApiError = {
  error?: { code?: string; message?: string; retryable?: boolean };
  server_draft?: Draft;
  local_draft?: Record<string, unknown>;
  current_revision?: number;
};

export class StudioApiError extends Error {
  status: number;
  payload: ApiError;

  constructor(status: number, payload: ApiError) {
    super(payload.error?.message ?? "The Studio request failed.");
    this.name = "StudioApiError";
    this.status = status;
    this.payload = payload;
  }
}

export async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    credentials: "same-origin",
    ...init,
    headers: {
      accept: "application/json",
      ...(init?.headers ?? {}),
    },
  });
  const contentType = response.headers.get("content-type") ?? "";
  // Session expired: fetch follows the 303 to /login and returns HTML 200.
  // Detect redirect, 401, or non-JSON and force a login navigation.
  if (response.redirected || response.status === 401 || !contentType.includes("application/json")) {
    try {
      window.location.assign("/login");
    } catch {
      /* test environment without window */
    }
    throw new StudioApiError(401, {
      error: { code: "unauthenticated", message: "Sign in to continue.", retryable: false },
    });
  }
  let payload: T & ApiError;
  try {
    payload = (await response.json()) as T & ApiError;
  } catch {
    try {
      window.location.assign("/login");
    } catch {
      /* ignore */
    }
    throw new StudioApiError(401, {
      error: { code: "unauthenticated", message: "Sign in to continue.", retryable: false },
    });
  }
  if (!response.ok) {
    throw new StudioApiError(response.status, payload);
  }
  return payload as T;
}

export function csrfToken(): string {
  return document.querySelector<HTMLMetaElement>("meta[name=studio-csrf-token]")?.content ?? "";
}

export function asThreadMessages(messages: PersistedMessage[]) {
  return messages
    .flatMap((message) => {
      if (message.role !== "user" && message.role !== "assistant") return [];
      return [{
        id: `persisted-${message.id}`,
        role: message.role,
        content: [{ type: "text" as const, text: message.content }],
      }];
    });
}
