import { describe, expect, it } from "vitest";
import { describeRunFailure } from "./runFailure";

describe("describeRunFailure", () => {
  it("prefers the safe API error body for a rejected run", () => {
    expect(describeRunFailure({
      message: "HTTP 409",
      payload: { error: { code: "provider_consent_required", message: "Allow OpenRouter above to start." } },
    })).toEqual({ code: "provider_consent_required", message: "Allow OpenRouter above to start." });
  });

  it("maps a terminal AG-UI error", () => {
    expect(describeRunFailure({ code: "provider_model_not_found", message: "The model 'x/y' is not available on OpenRouter." })).toEqual({
      code: "provider_model_not_found",
      message: "The model 'x/y' is not available on OpenRouter.",
    });
  });
});
