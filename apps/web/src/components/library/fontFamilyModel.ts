import type { MediaAssetRecord } from "../../api/client";

export type FontFaceOption = {
  asset: MediaAssetRecord;
  weight: number;
};

export type FontFamilyOption = {
  key: string;
  family: string;
  faces: FontFaceOption[];
};

const WEIGHT_NAMES: Array<[RegExp, number]> = [
  [/\b(thin|hairline)\b/i, 100],
  [/\b(extra[ -]?light|ultra[ -]?light)\b/i, 200],
  [/\blight\b/i, 300],
  [/\b(regular|normal|book)\b/i, 400],
  [/\bmedium\b/i, 500],
  [/\b(semi[ -]?bold|demi[ -]?bold)\b/i, 600],
  [/\b(extra[ -]?bold|ultra[ -]?bold)\b/i, 800],
  [/\bbold\b/i, 700],
  [/\b(black|heavy)\b/i, 900],
];

function tagValue(asset: MediaAssetRecord, prefix: string) {
  const tag = (asset.tags ?? []).find((item) => item.toLowerCase().startsWith(prefix));
  return tag?.slice(prefix.length).trim() ?? "";
}

function fallbackFamily(title: string) {
  const withoutExtension = title.replace(/\.(ttf|otf|ttc|woff2?)$/i, "").trim();
  return withoutExtension
    .replace(
      /(?:[ _-]+)(thin|hairline|extra[ _-]?light|ultra[ _-]?light|light|regular|normal|book|medium|semi[ _-]?bold|demi[ _-]?bold|bold|extra[ _-]?bold|ultra[ _-]?bold|black|heavy|[1-9]00)$/i,
      "",
    )
    .trim() || withoutExtension;
}

export function fontAssetFamily(asset: MediaAssetRecord) {
  return tagValue(asset, "family:") || fallbackFamily(asset.title) || asset.id;
}

export function fontAssetWeight(asset: MediaAssetRecord) {
  const tagged = Number(tagValue(asset, "weight:"));
  if (Number.isInteger(tagged) && tagged >= 1 && tagged <= 1000) return tagged;
  const numeric = asset.title.match(/(?:^|[ _-])([1-9]00)(?:$|[ _.-])/i);
  if (numeric) return Number(numeric[1]);
  for (const [pattern, weight] of WEIGHT_NAMES) {
    if (pattern.test(asset.title)) return weight;
  }
  return 400;
}

export function fontWeightLabel(weight: number) {
  if (weight <= 300) return "细体";
  if (weight <= 500) return "常规";
  if (weight <= 700) return "加粗";
  return "特粗";
}

export function groupFontFamilies(assets: MediaAssetRecord[]): FontFamilyOption[] {
  const groups = new Map<string, FontFamilyOption>();
  for (const asset of assets) {
    const family = fontAssetFamily(asset);
    const key = family.trim().toLocaleLowerCase();
    const group = groups.get(key) ?? { key, family, faces: [] };
    group.faces.push({ asset, weight: fontAssetWeight(asset) });
    groups.set(key, group);
  }
  return Array.from(groups.values())
    .map((group) => ({
      ...group,
      faces: group.faces.sort(
        (left, right) => left.weight - right.weight || left.asset.title.localeCompare(right.asset.title),
      ),
    }))
    .sort((left, right) => left.family.localeCompare(right.family, "zh-Hans-CN"));
}

export function familyForAsset(
  families: FontFamilyOption[],
  assetId: string | null | undefined,
) {
  if (!assetId) return null;
  return families.find((family) => family.faces.some((face) => face.asset.id === assetId)) ?? null;
}

export function faceForWeight(family: FontFamilyOption, preferredWeight: number) {
  return family.faces.reduce((best, face) => {
    const bestDistance = Math.abs(best.weight - preferredWeight);
    const distance = Math.abs(face.weight - preferredWeight);
    return distance < bestDistance ? face : best;
  });
}
