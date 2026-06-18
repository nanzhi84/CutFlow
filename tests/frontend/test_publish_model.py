from __future__ import annotations

import json
import subprocess


def _run_publish_model_probe() -> dict:
    script = r"""
import fs from "node:fs";
import ts from "typescript";

const source = fs.readFileSync("src/components/publish/publishModel.ts", "utf8");
const output = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2020, target: ts.ScriptTarget.ES2020 },
}).outputText;
const moduleUrl = `data:text/javascript;base64,${Buffer.from(output).toString("base64")}`;
const { displayFinishedVideoTitle, publishTitleForFinishedVideo } = await import(moduleUrl);

console.log(JSON.stringify({
  untitled: displayFinishedVideoTitle({ id: "fv_1", title: "", video_number: "V-001" }),
  named: displayFinishedVideoTitle({ id: "fv_2", title: "产品介绍", video_number: "V-002" }),
  fallback: displayFinishedVideoTitle({ id: "fv_abcdef123456", title: "" }),
  publishTitle: publishTitleForFinishedVideo({ title: " 产品介绍 " }),
  publishFallback: publishTitleForFinishedVideo({ title: "" }),
}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd="apps/web",
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


def test_finished_video_display_title_surfaces_stable_video_number() -> None:
    result = _run_publish_model_probe()

    assert result["untitled"] == "V-001 · 未命名成片"
    assert result["named"] == "V-002 · 产品介绍"
    assert result["fallback"] == "fv_abcdef12 · 未命名成片"
    assert result["publishTitle"] == "产品介绍"
    assert result["publishFallback"] == "未命名成片"
