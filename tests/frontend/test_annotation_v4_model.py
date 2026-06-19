from __future__ import annotations

import json
import subprocess


def _run_annotation_v4_probe() -> dict:
    script = r"""
import * as esbuild from "esbuild";

const probe = `
	import { bgmSegmentsToCanonical, canonicalToBgmSegments } from "./src/utils/annotationV4";
	
	const segments = canonicalToBgmSegments({
	  bgm_segments: [
	    {
	      segment_id: "seg_drop",
	      start: 12,
	      end: 24,
      duration: 12,
      role: "climax",
      drop_anchor_sec: 16,
      energy: 0.86,
      mood: "燃",
      scene_fit: ["转场"],
      avoid_scene: ["静态讲解"],
      reason: "drop clear",
      confidence: 0.91,
      source: "sensor+audio",
	    },
	  ],
	});
	const oldFieldSegments = canonicalToBgmSegments({
	  bgm_usage_windows: [{ segment_id: "old", start: 0, end: 1, role: "hook" }],
	});
	const canonical = bgmSegmentsToCanonical(segments);
	const editedCanonical = bgmSegmentsToCanonical([{ ...segments[0], end: 25.5 }]);
	
	console.log(JSON.stringify({
	  segment: segments[0],
	  oldFieldCount: oldFieldSegments.length,
	  canonical: canonical[0],
	  editedCanonical: editedCanonical[0],
	}));
`;
const result = esbuild.buildSync({
  stdin: {
    contents: probe,
    resolveDir: process.cwd(),
    sourcefile: "annotationV4Probe.ts",
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


def test_bgm_segments_preserve_hidden_fields_when_round_tripped_for_save() -> None:
    result = _run_annotation_v4_probe()

    assert result["segment"]["segment_id"] == "seg_drop"
    assert result["oldFieldCount"] == 0
    assert result["canonical"]["segment_id"] == "seg_drop"
    assert result["canonical"]["duration"] == 12
    assert result["editedCanonical"]["duration"] == 13.5
    assert result["canonical"]["avoid_scene"] == ["静态讲解"]
    assert result["canonical"]["confidence"] == 0.91
    assert result["canonical"]["source"] == "sensor+audio"
