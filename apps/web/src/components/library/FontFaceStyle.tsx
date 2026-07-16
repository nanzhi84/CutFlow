import { useEffect } from "react";
import { fontFamilyName } from "./libraryModel";

export function FontFaceStyle({
  assetId,
  url,
  weight = 400,
}: {
  assetId: string;
  url: string;
  weight?: number;
}) {
  const family = fontFamilyName(assetId);

  useEffect(() => {
    const face = new FontFace(family, `url(${JSON.stringify(url)})`, {
      weight: String(weight),
    });
    document.fonts.add(face);
    void face.load().catch(() => document.fonts.delete(face));

    return () => {
      document.fonts.delete(face);
    };
  }, [family, url, weight]);

  return null;
}
