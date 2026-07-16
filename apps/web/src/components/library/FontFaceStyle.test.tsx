import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FontFaceStyle } from "./FontFaceStyle";
import { fontFamilyName } from "./libraryModel";

type FakeFace = {
  family: string;
  source: string;
  descriptors?: FontFaceDescriptors;
  load: ReturnType<typeof vi.fn>;
};

describe("FontFaceStyle", () => {
  const add = vi.fn();
  const remove = vi.fn();
  const faces: FakeFace[] = [];

  beforeEach(() => {
    faces.length = 0;
    add.mockReset();
    remove.mockReset();
    vi.stubGlobal(
      "FontFace",
      class {
        family: string;
        source: string;
        load = vi.fn().mockResolvedValue(this);

        descriptors?: FontFaceDescriptors;

        constructor(family: string, source: string, descriptors?: FontFaceDescriptors) {
          this.family = family;
          this.source = source;
          this.descriptors = descriptors;
          faces.push(this);
        }
      },
    );
    Object.defineProperty(document, "fonts", {
      configurable: true,
      value: { add, delete: remove },
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    Reflect.deleteProperty(document, "fonts");
  });

  it("registers the selected asset and replaces it when the URL changes", async () => {
    const view = render(<FontFaceStyle assetId="font_a" url="/font-a.otf" weight={400} />);

    expect(faces).toHaveLength(1);
    expect(faces[0].family).toBe(fontFamilyName("font_a"));
    expect(faces[0].source).toBe('url("/font-a.otf")');
    expect(faces[0].descriptors).toEqual({ weight: "400" });
    expect(add).toHaveBeenCalledWith(faces[0]);
    await act(async () => {
      await faces[0].load.mock.results[0].value;
    });

    view.rerender(<FontFaceStyle assetId="font_b" url="/font-b.otf" weight={700} />);

    expect(remove).toHaveBeenCalledWith(faces[0]);
    expect(faces).toHaveLength(2);
    expect(faces[1].family).toBe(fontFamilyName("font_b"));
    expect(faces[1].source).toBe('url("/font-b.otf")');
    expect(faces[1].descriptors).toEqual({ weight: "700" });
    expect(add).toHaveBeenLastCalledWith(faces[1]);

    view.unmount();
    expect(remove).toHaveBeenCalledWith(faces[1]);
  });
});
