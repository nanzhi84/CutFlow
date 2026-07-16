import { describe, expect, it } from "vitest";

import type { MediaAssetRecord } from "../../api/client";
import {
  faceForWeight,
  fontAssetFamily,
  fontAssetWeight,
  groupFontFamilies,
} from "./fontFamilyModel";

function font(id: string, title: string, tags: string[]): MediaAssetRecord {
  return {
    id,
    title,
    kind: "font",
    tags,
    annotation_status: "annotated",
    usable: true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  } as MediaAssetRecord;
}

describe("fontFamilyModel", () => {
  it("groups regular and bold files into one selectable family", () => {
    const regular = font("serif_regular", "Noto Serif Regular", [
      "family:Noto Serif CJK SC",
      "weight:400",
    ]);
    const bold = font("serif_bold", "Noto Serif Bold", [
      "family:Noto Serif CJK SC",
      "weight:700",
    ]);

    const families = groupFontFamilies([bold, regular]);

    expect(families).toHaveLength(1);
    expect(families[0].family).toBe("Noto Serif CJK SC");
    expect(families[0].faces.map((face) => face.weight)).toEqual([400, 700]);
    expect(faceForWeight(families[0], 700).asset.id).toBe("serif_bold");
  });

  it("falls back to title metadata for user-uploaded font files", () => {
    const uploaded = font("brand_bold", "Brand Sans Bold.otf", ["font", "upload"]);

    expect(fontAssetFamily(uploaded)).toBe("Brand Sans");
    expect(fontAssetWeight(uploaded)).toBe(700);
  });
});
