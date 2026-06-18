from __future__ import annotations

import json
import subprocess


def _run_library_interaction_probe() -> dict:
    script = r"""
import * as esbuild from "esbuild";

const probe = `
import {
  addPendingIds,
  buildUploadPlaceholders,
  readCardThumbnailUrl,
  removePendingId,
  removePendingIds,
} from "./src/components/library/libraryInteractionModel";

const files = [
  { name: "alpha.mp4", lastModified: 11 },
  { name: "beta.mp4", lastModified: 22 },
  { name: "gamma.mp4", lastModified: 33 },
];
const placeholders = buildUploadPlaceholders(files, "video", (file, index) => "ph_" + index + "_" + file.name);
const pending = addPendingIds(new Set(["asset_a"]), ["asset_b", "asset_c", "asset_b"]);
const afterRemove = removePendingId(pending, "asset_b");
const afterBatchRemove = removePendingIds(pending, ["asset_a", "asset_c"]);
const topLevelThumb = readCardThumbnailUrl({
  thumbnail_url: "https://cdn.example/card.png",
  asset: { thumbnail_url: "https://cdn.example/asset.png" },
});
const assetThumb = readCardThumbnailUrl({
  thumbnail_url: null,
  asset: { thumbnail_url: "https://cdn.example/asset.png" },
});
const rejectedInternalThumb = readCardThumbnailUrl({
  thumbnail_url: "local://private/card.png",
  asset: { thumbnail_url: "local://private/asset.png" },
});
const safeAssetFallbackThumb = readCardThumbnailUrl({
  thumbnail_url: "local://private/card.png",
  asset: { thumbnail_url: "/api/media/assets/asset_a/preview-url" },
});

console.log(JSON.stringify({
  placeholderIds: placeholders.map((item) => item.id),
  placeholderNames: placeholders.map((item) => item.name),
  placeholderStatuses: placeholders.map((item) => item.status),
  pending: Array.from(pending).sort(),
  afterRemove: Array.from(afterRemove).sort(),
  afterBatchRemove: Array.from(afterBatchRemove).sort(),
  topLevelThumb,
  assetThumb,
  rejectedInternalThumb,
  safeAssetFallbackThumb,
}));
`;
const result = esbuild.buildSync({
  stdin: {
    contents: probe,
    resolveDir: process.cwd(),
    sourcefile: "libraryInteractionProbe.ts",
    loader: "ts",
  },
  bundle: true,
  write: false,
  format: "esm",
  platform: "node",
  target: "es2020",
});
await import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString("base64")}`);
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd="apps/web",
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


def test_upload_placeholders_and_pending_asset_state_are_per_item() -> None:
    result = _run_library_interaction_probe()

    assert result["placeholderIds"] == ["ph_0_alpha.mp4", "ph_1_beta.mp4", "ph_2_gamma.mp4"]
    assert result["placeholderNames"] == ["alpha.mp4", "beta.mp4", "gamma.mp4"]
    assert result["placeholderStatuses"] == ["uploading", "uploading", "uploading"]
    assert result["pending"] == ["asset_a", "asset_b", "asset_c"]
    assert result["afterRemove"] == ["asset_a", "asset_c"]
    assert result["afterBatchRemove"] == ["asset_b"]
    assert result["topLevelThumb"] == "https://cdn.example/card.png"
    assert result["assetThumb"] == "https://cdn.example/asset.png"
    assert result["rejectedInternalThumb"] is None
    assert result["safeAssetFallbackThumb"] == "/api/media/assets/asset_a/preview-url"
