from __future__ import annotations

import json
import subprocess


def _run_probe(probe: str) -> dict:
    script = (
        'import * as esbuild from "esbuild";\n'
        f"const probe = {json.dumps(probe)};\n"
        "const result = esbuild.buildSync({\n"
        "  stdin: {\n"
        "    contents: probe,\n"
        "    resolveDir: process.cwd(),\n"
        '    sourcefile: "probe.ts",\n'
        '    loader: "ts",\n'
        "  },\n"
        "  bundle: true,\n"
        "  write: false,\n"
        '  format: "esm",\n'
        '  platform: "node",\n'
        '  target: "es2020",\n'
        "});\n"
        "await import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString(\"base64\")}`);\n"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd="apps/web",
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


def test_form_defaults_round_trip_preserves_preference_blocks() -> None:
    probe = r"""
import { loadStoredForm, mapDefaultsToForm, mapFormToDefaults } from "./src/components/studio-create/studioCreateModel";

const baseForm = loadStoredForm();
const customForm = {
  ...baseForm,
  title: "should-be-ignored",
  script: "ignored body",
  scriptVersionId: "ver_x",
  voiceId: "voice_42",
  speed: 1.25,
  emotion: "energetic",
  brollEnabled: false,
  maxInserts: 7,
  subtitleEnabled: true,
  subtitleFontId: "font_yst",
  captionStylePairId: "local_promo_c",
  subtitleStyle: "news",
  subtitleSize: 36,
  bgmEnabled: true,
  bgmVolume: 0.5,
  bgmAutoMix: false,
  coverMode: "ai",
  lipsyncEnabled: false,
  lipsyncTimeoutMinutes: 45,
};

const defaults = mapFormToDefaults(customForm);
// defaults must NOT carry content (no title/script keys)
const defaultsKeys = Object.keys(defaults);
// hydrate a fresh base form from saved defaults
const hydrated = mapDefaultsToForm(defaults, baseForm);

console.log(JSON.stringify({
  defaultsKeys,
  voiceId: defaults.voice ? defaults.voice.voice_id : null,
  voiceSpeed: defaults.voice ? defaults.voice.speed : null,
  bgmVolume: defaults.bgm ? defaults.bgm.volume : null,
  bgmAutoMix: defaults.bgm ? defaults.bgm.auto_mix : null,
  subtitleFontId: defaults.subtitle ? defaults.subtitle.font_id : null,
  captionStylePairId: defaults.subtitle ? defaults.subtitle.caption_style_pair_id : null,
  coverMode: defaults.cover ? defaults.cover.mode : null,
  hydratedVoiceId: hydrated.voiceId,
  hydratedSpeed: hydrated.speed,
  hydratedEmotion: hydrated.emotion,
  hydratedBrollEnabled: hydrated.brollEnabled,
  hydratedMaxInserts: hydrated.maxInserts,
  hydratedSubtitleFontId: hydrated.subtitleFontId,
  hydratedCaptionStylePairId: hydrated.captionStylePairId,
  hydratedSubtitleStyle: hydrated.subtitleStyle,
  hydratedSubtitleSize: hydrated.subtitleSize,
  hydratedBgmEnabled: hydrated.bgmEnabled,
  hydratedBgmVolume: hydrated.bgmVolume,
  hydratedBgmAutoMix: hydrated.bgmAutoMix,
  hydratedCoverMode: hydrated.coverMode,
  hydratedLipsyncEnabled: hydrated.lipsyncEnabled,
  hydratedLipsyncTimeout: hydrated.lipsyncTimeoutMinutes,
  // content fields stay from base, not defaults
  hydratedTitle: hydrated.title,
  hydratedScript: hydrated.script,
}));
"""
    result = _run_probe(probe)
    assert "title" not in result["defaultsKeys"]
    assert "script" not in result["defaultsKeys"]
    assert result["voiceId"] == "voice_42"
    assert result["voiceSpeed"] == 1.25
    assert result["bgmVolume"] == 0.5
    assert result["bgmAutoMix"] is False
    assert result["subtitleFontId"] == "font_yst"
    assert result["captionStylePairId"] == "local_promo_c"
    assert result["coverMode"] == "ai"
    # round-trip hydration must reproduce the preference values
    assert result["hydratedVoiceId"] == "voice_42"
    assert result["hydratedSpeed"] == 1.25
    assert result["hydratedEmotion"] == "energetic"
    assert result["hydratedBrollEnabled"] is False
    assert result["hydratedMaxInserts"] == 7
    assert result["hydratedSubtitleFontId"] == "font_yst"
    assert result["hydratedCaptionStylePairId"] == "local_promo_c"
    assert result["hydratedSubtitleStyle"] == "news"
    assert result["hydratedSubtitleSize"] == 36
    assert result["hydratedBgmEnabled"] is True
    assert result["hydratedBgmVolume"] == 0.5
    assert result["hydratedBgmAutoMix"] is False
    assert result["hydratedCoverMode"] == "ai"
    assert result["hydratedLipsyncEnabled"] is False
    assert result["hydratedLipsyncTimeout"] == 45
    # base content untouched
    assert result["hydratedTitle"] == defaults_title_should_be_empty()
    assert result["hydratedScript"] == defaults_script_from_base()


def test_seedance_reference_assets_are_optional() -> None:
    probe = r"""
import { loadStoredForm, validateAll, validateStep } from "./src/components/studio-create/studioCreateModel";

const baseForm = loadStoredForm();
const seedanceForm = {
  ...baseForm,
  contentMode: "seedance",
  seedanceReferenceAssetIds: [],
  script: "用纯文本生成一条 15 秒短视频",
};

console.log(JSON.stringify({
  templateStep: validateStep(1, seedanceForm, ""),
  productionStep: validateStep(2, seedanceForm, ""),
  postProcessStep: validateStep(3, { ...seedanceForm, subtitleSize: 999, bgmVolume: 999 }, ""),
  all: validateAll(seedanceForm, ""),
}));
"""
    result = _run_probe(probe)
    assert result["templateStep"] is None
    assert result["productionStep"] is None
    assert result["postProcessStep"] is None
    assert result["all"] is None


def defaults_title_should_be_empty() -> str:
    return ""


def defaults_script_from_base() -> str:
    return ""


def test_batch_request_builds_one_item_per_script() -> None:
    probe = r"""
import { buildBatchRequest } from "./src/components/studio-create/batchModel";

const items = [
  { script: " 脚本一 ", title: "标题一", scriptVersionId: "v1" },
  { script: "脚本二", title: null, scriptVersionId: null },
  { script: "脚本三", title: "标题三" },
];

const request = buildBatchRequest("case_77", items, true);

console.log(JSON.stringify({
  schemaVersion: request.schema_version,
  caseId: request.case_id,
  useMyDefaults: request.use_my_defaults,
  count: request.items.length,
  firstScript: request.items[0].script,
  firstTitle: request.items[0].title,
  firstVersion: request.items[0].script_version_id,
  secondTitle: request.items[1].title,
  thirdVersion: request.items[2].script_version_id,
}));
"""
    result = _run_probe(probe)
    assert result["schemaVersion"] == "batch_digital_human_video_request.v1"
    assert result["caseId"] == "case_77"
    assert result["useMyDefaults"] is True
    assert result["count"] == 3
    # whitespace trimmed
    assert result["firstScript"] == "脚本一"
    assert result["firstTitle"] == "标题一"
    assert result["firstVersion"] == "v1"
    assert result["secondTitle"] is None
    assert result["thirdVersion"] is None


def test_parse_pasted_scripts_splits_on_blank_lines() -> None:
    probe = r"""
import { parsePastedScripts } from "./src/components/studio-create/batchModel";

const raw = "第一条脚本\n第一条第二行\n\n第二条脚本\n\n\n  \n第三条脚本";
const blocks = parsePastedScripts(raw);
console.log(JSON.stringify({ count: blocks.length, blocks }));
"""
    result = _run_probe(probe)
    assert result["count"] == 3
    assert result["blocks"][0] == "第一条脚本\n第一条第二行"
    assert result["blocks"][1] == "第二条脚本"
    assert result["blocks"][2] == "第三条脚本"


def test_terminal_transition_summary_merges_many_into_one_message() -> None:
    probe = r"""
import { summarizeTerminalTransitions } from "./src/hooks/notificationModel";

const previous = new Map([
  ["r1", "running"],
  ["r2", "running"],
  ["r3", "running"],
  ["r4", "succeeded"],
]);

const runs = [
  { runId: "r1", title: "甲", status: "succeeded" },
  { runId: "r2", title: "乙", status: "succeeded" },
  { runId: "r3", title: "丙", status: "failed" },
  { runId: "r4", title: "丁", status: "succeeded" },
  { runId: "r5", title: "戊", status: "running" },
];

const summary = summarizeTerminalTransitions(runs, previous);

console.log(JSON.stringify({
  succeeded: summary.succeeded,
  failed: summary.failed,
  hasNotification: summary.notification !== null,
  notificationTitle: summary.notification ? summary.notification.title : null,
  notificationBody: summary.notification ? summary.notification.body : null,
  transitionsCount: summary.transitions.length,
}));
"""
    result = _run_probe(probe)
    # r1, r2 succeeded; r3 failed; r4 was already terminal (no transition); r5 still running
    assert result["succeeded"] == 2
    assert result["failed"] == 1
    assert result["transitionsCount"] == 3
    # many terminal transitions collapse into a single notification
    assert result["hasNotification"] is True
    assert "2" in result["notificationBody"]
    assert "1" in result["notificationBody"]


def test_single_terminal_transition_names_the_run() -> None:
    probe = r"""
import { summarizeTerminalTransitions } from "./src/hooks/notificationModel";

const previous = new Map([["r1", "running"]]);
const runs = [{ runId: "r1", title: "唯一任务", status: "succeeded" }];
const summary = summarizeTerminalTransitions(runs, previous);
console.log(JSON.stringify({
  hasNotification: summary.notification !== null,
  body: summary.notification ? summary.notification.body : null,
  succeeded: summary.succeeded,
}));
"""
    result = _run_probe(probe)
    assert result["hasNotification"] is True
    assert result["succeeded"] == 1
    assert "唯一任务" in result["body"]


def test_no_transition_yields_no_notification() -> None:
    probe = r"""
import { summarizeTerminalTransitions } from "./src/hooks/notificationModel";

const previous = new Map([["r1", "succeeded"], ["r2", "running"]]);
const runs = [
  { runId: "r1", title: "甲", status: "succeeded" },
  { runId: "r2", title: "乙", status: "running" },
];
const summary = summarizeTerminalTransitions(runs, previous);
console.log(JSON.stringify({ hasNotification: summary.notification !== null, count: summary.transitions.length }));
"""
    result = _run_probe(probe)
    assert result["hasNotification"] is False
    assert result["count"] == 0
