from __future__ import annotations

import json
import subprocess


def _run_dropzone_model_probe() -> dict:
    script = r"""
import fs from "node:fs";
import ts from "typescript";

const source = fs.readFileSync("src/components/ui/dropZoneModel.ts", "utf8");
const output = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2020, target: ts.ScriptTarget.ES2020 },
}).outputText;
const moduleUrl = `data:text/javascript;base64,${Buffer.from(output).toString("base64")}`;
const { resolveAcceptedDropFiles } = await import(moduleUrl);
const currentFiles = [{ name: "first.mp4", type: "video/mp4", size: 1 }];
const incomingFiles = [
  { name: "second.mov", type: "video/quicktime", size: 1 },
  { name: "third.mp4", type: "video/mp4", size: 1 },
];
const batch = resolveAcceptedDropFiles(incomingFiles, {
  accept: ".mp4,.mov",
  maxSizeMb: 500,
  multiple: true,
  currentFiles,
});
const single = resolveAcceptedDropFiles(incomingFiles, {
  accept: ".mp4,.mov",
  maxSizeMb: 500,
  multiple: false,
  currentFiles,
});
console.log(JSON.stringify({
  batchNames: batch.files.map((file) => file.name),
  acceptedNames: batch.acceptedFiles.map((file) => file.name),
  singleNames: single.files.map((file) => file.name),
  batchError: batch.error,
  singleError: single.error,
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


def test_dropzone_multiple_mode_reports_the_full_accumulated_selection() -> None:
    result = _run_dropzone_model_probe()

    assert result["batchNames"] == ["first.mp4", "second.mov", "third.mp4"]
    assert result["acceptedNames"] == ["second.mov", "third.mp4"]
    assert result["singleNames"] == ["second.mov"]
    assert result["batchError"] is None
    assert result["singleError"] is None
