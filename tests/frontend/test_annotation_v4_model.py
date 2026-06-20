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
      section_type: "chorus",
      section_label: "B",
      repeat_group: "chorus_1",
      loopable: true,
      energy_profile: "rising",
      script_fit: ["硬广开场"],
      avoid_script: ["睡眠放松"],
      reason: "drop clear",
      confidence: 0.91,
      source: "sensor+audio",
	    },
	  ],
	});
		const canonical = bgmSegmentsToCanonical(segments);
		const editedCanonical = bgmSegmentsToCanonical([{ ...segments[0], end: 25.5 }]);
		
		console.log(JSON.stringify({
		  segment: segments[0],
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
    assert result["canonical"]["segment_id"] == "seg_drop"
    assert result["canonical"]["duration"] == 12
    assert result["editedCanonical"]["duration"] == 13.5
    assert result["canonical"]["avoid_scene"] == ["静态讲解"]
    assert result["canonical"]["section_type"] == "chorus"
    assert result["canonical"]["section_label"] == "B"
    assert result["canonical"]["repeat_group"] == "chorus_1"
    assert result["canonical"]["loopable"] is True
    assert result["canonical"]["energy_profile"] == "rising"
    assert result["canonical"]["script_fit"] == ["硬广开场"]
    assert result["canonical"]["avoid_script"] == ["睡眠放松"]
    assert result["canonical"]["confidence"] == 0.91
    assert result["canonical"]["source"] == "sensor+audio"
