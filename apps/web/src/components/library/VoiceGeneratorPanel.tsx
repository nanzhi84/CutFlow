import { Download, Loader2, Play } from "lucide-react";
import { useEffect, useState } from "react";
import type { VoiceProfile } from "../../api/client";
import { voiceSourceLabels } from "./libraryModel";

type VoiceGeneratorPanelProps = {
  voices: VoiceProfile[];
  selectedVoiceId: string;
  previewText: string;
  previewUri: string | null;
  previewDuration: number | null;
  isPreviewing: boolean;
  onTextChange: (value: string) => void;
  onPreview: (voice: VoiceProfile) => void;
};

export function VoiceGeneratorPanel({
  voices,
  selectedVoiceId,
  previewText,
  previewUri,
  previewDuration,
  isPreviewing,
  onTextChange,
  onPreview,
}: VoiceGeneratorPanelProps) {
  const [voiceId, setVoiceId] = useState(selectedVoiceId);
  const selectedVoice = voices.find((voice) => voice.id === voiceId) ?? voices[0] ?? null;

  useEffect(() => {
    if (!voiceId && selectedVoiceId) setVoiceId(selectedVoiceId);
  }, [selectedVoiceId, voiceId]);

  return (
    <aside className="card grid content-start gap-4">
      <div>
        <h2 className="text-lg font-semibold text-text-primary">生成音频</h2>
        <p className="mt-1 text-sm text-text-secondary">使用音色绑定的 provider 配置生成试听。</p>
      </div>
      <label>
        <span>试听文本</span>
        <textarea value={previewText} onChange={(event) => onTextChange(event.target.value)} className="min-h-[112px]" maxLength={300} />
      </label>
      <label>
        <span>音色</span>
        <select value={voiceId} onChange={(event) => setVoiceId(event.target.value)}>
          {voices.map((voice) => (
            <option key={voice.id} value={voice.id}>
              {voice.display_name}（{voiceSourceLabels[voice.source]}）
            </option>
          ))}
        </select>
      </label>
      <button className="btn-primary w-full" type="button" disabled={!selectedVoice || isPreviewing} onClick={() => selectedVoice && onPreview(selectedVoice)}>
        {isPreviewing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
        <span>{isPreviewing ? "生成中" : "试听生成"}</span>
      </button>
      {previewUri ? (
        <div className="grid gap-3 rounded-2xl border border-border/80 bg-white/65 p-3">
          <audio src={previewUri} controls className="w-full" />
          <div className="flex items-center justify-between gap-3 text-xs text-text-secondary">
            <span>{previewDuration ? `约 ${Math.round(previewDuration)} 秒` : "试听音频"}</span>
            <a className="btn-secondary min-h-9 px-3" href={previewUri} download>
              <Download className="h-4 w-4" />
              <span>下载</span>
            </a>
          </div>
        </div>
      ) : null}
    </aside>
  );
}
