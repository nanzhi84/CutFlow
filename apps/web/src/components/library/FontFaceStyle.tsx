import { useEffect } from "react";
import { fontFamilyName } from "./libraryModel";

export function FontFaceStyle({ assetId, url }: { assetId: string; url: string }) {
  const family = fontFamilyName(assetId);

  useEffect(() => {
    const face = new FontFace(family, `url(${JSON.stringify(url)})`);
    document.fonts.add(face);
    void face.load().catch(() => document.fonts.delete(face));

    return () => {
      document.fonts.delete(face);
    };
  }, [family, url]);

  return null;
}
