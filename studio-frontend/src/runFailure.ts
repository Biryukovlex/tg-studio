export type RunFailure = {
  code: string;
  message: string;
};

type FailureLike = {
  code?: unknown;
  message?: unknown;
  payload?: { error?: { code?: unknown; message?: unknown } };
};

export function describeRunFailure(error: unknown): RunFailure {
  const candidate = (error && typeof error === "object" ? error : {}) as FailureLike;
  const payloadError = candidate.payload?.error;
  const code = typeof payloadError?.code === "string"
    ? payloadError.code
    : typeof candidate.code === "string" ? candidate.code : "agent_failed";
  const message = typeof payloadError?.message === "string"
    ? payloadError.message
    : typeof candidate.message === "string" && candidate.message.trim()
      ? candidate.message
      : "The agent could not complete this run. Try again.";
  return { code, message };
}
