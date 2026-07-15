import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { RunDetailResponse } from "../../api/client";
import { buildCaptionComposition, CaptionDisplayPanel } from "./CaptionDisplayPanel";

function detail(): RunDetailResponse {
  return {
    artifacts: [{ artifact_id: "artifact_caption", kind: "plan.caption_composition" }],
    artifact_payloads: {
      artifact_caption: {
        policy_version: "caption_composition_v1",
        fps: 30,
        normal_enabled: true,
        emphasis_enabled: true,
        band: {
          anchor_x: 0.5,
          baseline_y: 0.84,
          line_height_ratio: 1.12,
          max_width_ratio: 0.85,
        },
        diagnostics: {
          timing_source: "asr_anchored",
          font_metrics_source: "hmtx",
          emphasis_font_metrics_source: "hmtx",
          hints_total: 2,
          hints_applied: 1,
          hints_token_unmatched: 1,
          fallbacks: [
            { reason: "token_unmatched", hint_ids: ["hint_0002"], phrase: "未命中" },
          ],
        },
        cues: [
          {
            cue_id: "cue_0001",
            text: "普通重点",
            start_frame: 0,
            end_frame: 30,
            lines: [
              {
                runs: [
                  {
                    run_id: "run_0001",
                    text: "重点",
                    role: "emphasis",
                    hint_id: "hint_0001",
                    token_ids: ["token_3", "token_4"],
                    char_span: [2, 4],
                    enter_frame: 8,
                    exit_frame: 30,
                    effect_id: "pop",
                    advance_px: 58,
                    baseline_offset_px: 55,
                  },
                ],
              },
            ],
          },
        ],
      },
    },
  } as unknown as RunDetailResponse;
}

describe("CaptionDisplayPanel", () => {
  it("shows authoritative run timing, token ownership and fallback details", () => {
    const plan = buildCaptionComposition(detail());
    render(<CaptionDisplayPanel plan={plan} />);

    expect(screen.getByText(/emphasis · f8–f30 · char\[2,4\]/)).toHaveTextContent(
      "token token_3,token_4 · hint_0001 · pop",
    );
    expect(screen.getByTestId("caption-fallbacks")).toHaveTextContent("token_unmatched");
    expect(screen.getByTestId("caption-fallbacks")).toHaveTextContent("hint_0002");
  });
});
