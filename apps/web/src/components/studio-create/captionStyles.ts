export type CaptionStylePairId = "douyin_bold_a" | "clean_editorial_b" | "local_promo_c";

export type LegacySubtitleStyle =
  | "douyin"
  | "clean"
  | "variety"
  | "news"
  | "movie"
  | "youshe_title_black";

export type CaptionPreviewStyle = {
  fontWeight: number;
  sizeScale: number;
  color: string;
  outlineColor: string;
  outline: number;
  position: { x: number; y: number };
};

export type HuaziPreviewStyle = Omit<CaptionPreviewStyle, "position"> & {
  defaultPlacementId: string;
  defaultAnimationId: string;
};

export const captionStylePairs: Record<
  CaptionStylePairId,
  {
    label: string;
    legacyStyle: LegacySubtitleStyle;
    normal: CaptionPreviewStyle;
    huazi: HuaziPreviewStyle;
  }
> = {
  douyin_bold_a: {
    label: "抖音高亮",
    legacyStyle: "douyin",
    normal: {
      fontWeight: 600,
      sizeScale: 1,
      color: "#FFFFFF",
      outlineColor: "#000000",
      outline: 4,
      position: { x: 0.5, y: 0.88 },
    },
    huazi: {
      fontWeight: 900,
      sizeScale: 1.45,
      color: "#FFE84A",
      outlineColor: "#000000",
      outline: 5,
      defaultPlacementId: "top_center_banner",
      defaultAnimationId: "pop_in",
    },
  },
  clean_editorial_b: {
    label: "清爽资讯",
    legacyStyle: "clean",
    normal: {
      fontWeight: 500,
      sizeScale: 1,
      color: "#F8FAFC",
      outlineColor: "#111827",
      outline: 3,
      position: { x: 0.5, y: 0.86 },
    },
    huazi: {
      fontWeight: 700,
      sizeScale: 1.28,
      color: "#BAE6FD",
      outlineColor: "#0F172A",
      outline: 4,
      defaultPlacementId: "upper_left_badge",
      defaultAnimationId: "fade_in",
    },
  },
  local_promo_c: {
    label: "本地促销",
    legacyStyle: "news",
    normal: {
      fontWeight: 600,
      sizeScale: 1,
      color: "#FFFFFF",
      outlineColor: "#111111",
      outline: 4.5,
      position: { x: 0.5, y: 0.89 },
    },
    huazi: {
      fontWeight: 900,
      sizeScale: 1.38,
      color: "#FF6B35",
      outlineColor: "#FFFFFF",
      outline: 3.5,
      defaultPlacementId: "upper_right_badge",
      defaultAnimationId: "punch",
    },
  },
};

export const captionStylePairOptions = Object.entries(captionStylePairs).map(([value, config]) => ({
  value: value as CaptionStylePairId,
  label: config.label,
}));

const legacyPairMap: Record<LegacySubtitleStyle, CaptionStylePairId> = {
  douyin: "douyin_bold_a",
  variety: "douyin_bold_a",
  youshe_title_black: "douyin_bold_a",
  clean: "clean_editorial_b",
  movie: "clean_editorial_b",
  news: "local_promo_c",
};

export function isCaptionStylePairId(value: unknown): value is CaptionStylePairId {
  return typeof value === "string" && value in captionStylePairs;
}

export function isLegacySubtitleStyle(value: unknown): value is LegacySubtitleStyle {
  return typeof value === "string" && value in legacyPairMap;
}

export function captionPairLabel(value: CaptionStylePairId) {
  return captionStylePairs[value]?.label ?? captionStylePairs.douyin_bold_a.label;
}

export function legacyStyleForCaptionPair(value: CaptionStylePairId): LegacySubtitleStyle {
  return captionStylePairs[value]?.legacyStyle ?? "douyin";
}

export function captionPairFromLegacyStyle(value: unknown): CaptionStylePairId {
  return isLegacySubtitleStyle(value) ? legacyPairMap[value] : "douyin_bold_a";
}
