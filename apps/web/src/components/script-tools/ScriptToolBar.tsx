import { History, ListPlus, Sparkles, Wand2 } from "lucide-react";
import type { ScriptToolMode } from "./scriptToolModel";

type Props = {
  candidateCount: number;
  historyCount: number;
  onOpenGenerate: (mode: ScriptToolMode) => void;
  onOpenCandidates: () => void;
  onOpenHistory: () => void;
};

export function ScriptToolBar({ candidateCount, historyCount, onOpenGenerate, onOpenCandidates, onOpenHistory }: Props) {
  return (
    <div className="flex flex-wrap items-center gap-2 border-y border-border/60 py-2">
      <button className="btn-secondary text-sm" type="button" onClick={() => onOpenGenerate("generate")}>
        <Sparkles className="h-4 w-4" />
        <span>AI 生成</span>
      </button>
      <button className="btn-secondary text-sm" type="button" onClick={() => onOpenGenerate("polish")}>
        <Wand2 className="h-4 w-4" />
        <span>润色</span>
      </button>
      <button className="btn-secondary relative text-sm" type="button" onClick={onOpenCandidates}>
        <ListPlus className="h-4 w-4" />
        <span>候选池</span>
        {candidateCount > 0 ? <Counter value={candidateCount} /> : null}
      </button>
      <button className="btn-secondary text-sm" type="button" onClick={onOpenHistory}>
        <History className="h-4 w-4" />
        <span>历史 {historyCount > 0 ? historyCount : ""}</span>
      </button>
    </div>
  );
}

function Counter({ value }: { value: number }) {
  return (
    <span className="absolute -right-1 -top-1 flex min-h-[18px] min-w-[18px] items-center justify-center rounded-full bg-accent px-1 text-[10px] font-semibold text-[#1b1d1a]">
      {value}
    </span>
  );
}
