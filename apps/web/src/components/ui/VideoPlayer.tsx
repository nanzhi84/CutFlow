import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Maximize, Pause, Play, Volume2, VolumeX } from "lucide-react";

// Controlled HTML5 video player with timeline visualization.
//
// Pure presentational, design-token driven (accent / status-* / brand from tailwind.config.js).
// Draws `segments` as clip bars on the progress track and `qualityEvents` as risk markers; both are
// clickable to seek. Highlights the segment under the playhead, and exposes onTimeUpdate / onSeek.
// Shared timeline visualization pattern adapted to the current design tokens.

export interface VideoPlayerSegment {
  /** Stable key for highlight + callback identity (e.g. clip segment_id). */
  id?: string;
  start: number;
  end: number;
  label?: string;
  /** Role drives the bar color (hook/main/backup/avoid/cover); falls back to a neutral accent tint. */
  role?: string;
}

export interface VideoPlayerQualityEvent {
  id?: string;
  start: number;
  end?: number;
  label?: string;
  /** "hard" => error tint, otherwise warning tint. */
  risk_tier?: string;
}

export interface VideoPlayerSeekRequest {
  time: number;
  key: number;
}

interface VideoPlayerEvidenceFrame {
  /** Timestamp (seconds) of the sampled evidence frame. */
  time: number;
  /** Optional thumbnail shown on hover; absent => render a plain tick only. */
  image_url?: string;
}

export interface VideoPlayerProps {
  src: string;
  poster?: string;
  className?: string;
  autoPlay?: boolean;
  preload?: "none" | "metadata" | "auto";
  segments?: VideoPlayerSegment[];
  qualityEvents?: VideoPlayerQualityEvent[];
  /** Evidence-frame ticks on the progress track (hover thumbnail when `image_url` present). */
  evidenceFrames?: VideoPlayerEvidenceFrame[];
  /** When provided, overrides the player's own loadedmetadata duration (e.g. canonical meta.duration). */
  durationHint?: number;
  /** Currently highlighted segment id (controlled selection); the player also auto-highlights the playhead segment. */
  activeSegmentId?: string | null;
  /** Imperative seek request from an external timeline/list item. */
  seekRequest?: VideoPlayerSeekRequest | null;
  /** Disable segment bar click handling when bars are visual overlays above a scrubber. */
  segmentBarsInteractive?: boolean;
  onTimeUpdate?: (time: number) => void;
  onDurationChange?: (duration: number) => void;
  /** Fired on any user-initiated seek (scrubber, segment/marker click). */
  onSeek?: (time: number) => void;
  /** Fired when a segment bar is clicked (in addition to onSeek). */
  onSegmentClick?: (segment: VideoPlayerSegment) => void;
  /** Fired when a quality-event marker is clicked (in addition to onSeek). */
  onQualityEventClick?: (event: VideoPlayerQualityEvent) => void;
}

// Role -> bar color. Uses raw token hex so inline styles stay in sync with tailwind.config.js.
const ROLE_COLORS: Record<string, string> = {
  hook: "#d6ff48", // brand.amber
  main: "#5e6d51", // accent
  backup: "#6f8a66", // brand.mint
  cover: "#9cb4a2", // brand.cyan
  avoid: "#c56a5d", // status.error
  climax: "#ef4444",
  outro: "#22c55e",
  general: "#3b82f6",
};
const SEGMENT_FALLBACK_COLOR = "#5e6d51";
const HARD_RISK_COLOR = "#c56a5d"; // status.error
const SOFT_RISK_COLOR = "#b68f32"; // status.warning

function formatClock(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) seconds = 0;
  const total = Math.floor(seconds);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function roleColor(role?: string): string {
  if (!role) return SEGMENT_FALLBACK_COLOR;
  return ROLE_COLORS[role.toLowerCase()] ?? SEGMENT_FALLBACK_COLOR;
}

export function VideoPlayer({
  src,
  poster,
  className = "",
  autoPlay = false,
  preload = "metadata",
  segments = [],
  qualityEvents = [],
  evidenceFrames = [],
  durationHint,
  activeSegmentId = null,
  seekRequest = null,
  segmentBarsInteractive = true,
  onTimeUpdate,
  onDurationChange,
  onSeek,
  onSegmentClick,
  onQualityEventClick,
}: VideoPlayerProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const lastSeekRequestKeyRef = useRef<number | null>(null);
  const [isPlaying, setIsPlaying] = useState(autoPlay);
  const [currentTime, setCurrentTime] = useState(0);
  const [mediaDuration, setMediaDuration] = useState(0);
  const [volume, setVolume] = useState(1);
  const [isMuted, setIsMuted] = useState(false);
  const [isHoveringProgress, setIsHoveringProgress] = useState(false);
  const [hoverTime, setHoverTime] = useState(0);
  const [hoverFrameIndex, setHoverFrameIndex] = useState<number | null>(null);

  const duration = useMemo(() => {
    const hint = durationHint && durationHint > 0 ? durationHint : 0;
    return Math.max(mediaDuration, hint) || 0;
  }, [mediaDuration, durationHint]);

  const seekTo = useCallback(
    (time: number) => {
      const video = videoRef.current;
      const clamped = Math.max(0, duration > 0 ? Math.min(duration, time) : time);
      if (video) video.currentTime = clamped;
      setCurrentTime(clamped);
      onSeek?.(clamped);
    },
    [duration, onSeek],
  );

  useEffect(() => {
    if (!seekRequest) return;
    if (lastSeekRequestKeyRef.current === seekRequest.key) return;
    lastSeekRequestKeyRef.current = seekRequest.key;
    seekTo(seekRequest.time);
  }, [seekRequest, seekTo]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;

    const handleTime = () => {
      setCurrentTime(video.currentTime);
      onTimeUpdate?.(video.currentTime);
    };
    const handleDuration = () => {
      setMediaDuration(video.duration || 0);
      onDurationChange?.(video.duration || 0);
    };
    const handlePlay = () => setIsPlaying(true);
    const handlePause = () => setIsPlaying(false);
    const handleEnded = () => setIsPlaying(false);

    video.addEventListener("timeupdate", handleTime);
    video.addEventListener("loadedmetadata", handleDuration);
    video.addEventListener("durationchange", handleDuration);
    video.addEventListener("play", handlePlay);
    video.addEventListener("pause", handlePause);
    video.addEventListener("ended", handleEnded);
    return () => {
      video.removeEventListener("timeupdate", handleTime);
      video.removeEventListener("loadedmetadata", handleDuration);
      video.removeEventListener("durationchange", handleDuration);
      video.removeEventListener("play", handlePlay);
      video.removeEventListener("pause", handlePause);
      video.removeEventListener("ended", handleEnded);
    };
  }, [onTimeUpdate, onDurationChange]);

  // Reset transport state when the source changes.
  useEffect(() => {
    setCurrentTime(0);
    setMediaDuration(0);
    setIsPlaying(autoPlay);
  }, [src, autoPlay]);

  const currentSegment = useMemo(() => {
    return segments.find((seg) => currentTime >= seg.start && currentTime <= seg.end) ?? null;
  }, [segments, currentTime]);

  const activeEvent = useMemo(() => {
    return (
      qualityEvents.find((ev) => {
        const end = ev.end ?? ev.start;
        return currentTime >= ev.start && currentTime <= end;
      }) ?? null
    );
  }, [qualityEvents, currentTime]);

  function togglePlay() {
    const video = videoRef.current;
    if (!video) return;
    if (video.paused) {
      const p = video.play();
      if (p) p.catch(() => setIsPlaying(false));
    } else {
      video.pause();
    }
  }

  function toggleMute() {
    const video = videoRef.current;
    if (!video) return;
    const next = !isMuted;
    video.muted = next;
    setIsMuted(next);
  }

  function toggleFullscreen() {
    const el = containerRef.current;
    if (!el) return;
    if (!document.fullscreenElement) {
      void el.requestFullscreen?.();
    } else {
      void document.exitFullscreen?.();
    }
  }

  function handleScrubberChange(e: React.ChangeEvent<HTMLInputElement>) {
    seekTo(parseFloat(e.target.value));
  }

  function handleTimelineClick(e: React.MouseEvent<HTMLDivElement>) {
    if (duration <= 0) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const ratio = rect.width > 0 ? (e.clientX - rect.left) / rect.width : 0;
    seekTo(Math.max(0, Math.min(1, ratio)) * duration);
  }

  function handleVolumeChange(e: React.ChangeEvent<HTMLInputElement>) {
    const vol = parseFloat(e.target.value);
    const video = videoRef.current;
    if (video) video.volume = vol;
    setVolume(vol);
    setIsMuted(vol === 0);
  }

  function handleProgressHover(e: React.MouseEvent<HTMLDivElement>) {
    const rect = e.currentTarget.getBoundingClientRect();
    const pct = (e.clientX - rect.left) / rect.width;
    setHoverTime(pct * duration);
    setIsHoveringProgress(true);
  }

  const pct = (time: number) => (duration > 0 ? Math.min(100, Math.max(0, (time / duration) * 100)) : 0);

  return (
    <div
      ref={containerRef}
      className={`group relative overflow-hidden rounded-2xl bg-black ${className}`}
    >
      <video
        ref={videoRef}
        src={src}
        poster={poster}
        autoPlay={autoPlay}
        preload={preload}
        playsInline
        className="h-full w-full bg-black object-contain"
        onClick={togglePlay}
      />

      {/* Risk banner for the event under the playhead */}
      {activeEvent ? (
        <div className="absolute inset-x-3 top-3 flex items-center gap-2 rounded-xl bg-status-error/90 px-3 py-2 text-xs font-medium text-white shadow-glow">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          <span>{(activeEvent.risk_tier ?? "").toLowerCase() === "soft" ? "软风险" : "硬风险"}</span>
          <span className="opacity-70">·</span>
          <span className="truncate opacity-90">{activeEvent.label || activeEvent.id || "质量事件"}</span>
        </div>
      ) : null}

      {/* Center play affordance when paused */}
      {!isPlaying ? (
        <button
          type="button"
          onClick={togglePlay}
          className="absolute inset-0 flex items-center justify-center bg-black/20 transition-colors"
          aria-label="播放"
        >
          <span className="grid h-16 w-16 place-items-center rounded-full bg-accent/90 text-white transition-transform hover:scale-110">
            <Play className="h-7 w-7 translate-x-0.5" />
          </span>
        </button>
      ) : null}

      {/* Control bar */}
      <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/85 via-black/45 to-transparent p-4 opacity-100 transition-opacity duration-300">
        {/* Timeline with segment bars + quality markers */}
        <div
          className="relative mb-3 h-3 cursor-pointer"
          onMouseMove={handleProgressHover}
          onMouseLeave={() => setIsHoveringProgress(false)}
          onClick={handleTimelineClick}
        >
          {/* Track */}
          <div className="absolute inset-x-0 top-1/2 h-2 -translate-y-1/2 overflow-hidden rounded-full bg-white/20">
            {/* Played portion */}
            <div className="absolute inset-y-0 left-0 bg-white/35" style={{ width: `${pct(currentTime)}%` }} />
          </div>

          {/* Segment bars */}
          {duration > 0
            ? segments.map((seg, i) => {
                const isActive = (activeSegmentId && seg.id === activeSegmentId) || seg === currentSegment;
                return (
                  <button
                    key={seg.id ?? `seg-${i}`}
                    type="button"
                    className={`absolute top-1/2 h-2 -translate-y-1/2 rounded-full transition-all ${
                      segmentBarsInteractive ? "hover:h-3" : "pointer-events-none"
                    }`}
                    title={seg.label || seg.id || `片段 ${i + 1}`}
                    tabIndex={segmentBarsInteractive ? 0 : -1}
                    aria-hidden={segmentBarsInteractive ? undefined : true}
                    onClick={
                      segmentBarsInteractive
                        ? (e) => {
                            e.stopPropagation();
                            seekTo(seg.start);
                            onSegmentClick?.(seg);
                          }
                        : undefined
                    }
                    style={{
                      left: `${pct(seg.start)}%`,
                      width: `${Math.max(0.5, pct(seg.end) - pct(seg.start))}%`,
                      backgroundColor: roleColor(seg.role),
                      opacity: isActive ? 1 : 0.7,
                      outline: isActive ? "1px solid rgba(255,255,255,0.85)" : "none",
                      zIndex: segmentBarsInteractive ? (isActive ? 5 : 3) : 2,
                    }}
                  />
                );
              })
            : null}

          {/* Quality-event markers */}
          {duration > 0
            ? qualityEvents.map((ev, i) => (
                <button
                  key={ev.id ?? `qe-${i}`}
                  type="button"
                  className="absolute top-1/2 h-3.5 w-1 -translate-x-1/2 -translate-y-1/2 rounded-full transition-transform hover:scale-y-125"
                  title={ev.label || ev.id || "质量事件"}
                  onClick={(e) => {
                    e.stopPropagation();
                    seekTo(ev.start);
                    onQualityEventClick?.(ev);
                  }}
                  style={{
                    left: `${pct(ev.start)}%`,
                    backgroundColor: (ev.risk_tier ?? "").toLowerCase() === "soft" ? SOFT_RISK_COLOR : HARD_RISK_COLOR,
                    zIndex: 6,
                  }}
                />
              ))
            : null}

          {/* Evidence-frame ticks (hover => thumbnail when available) */}
          {duration > 0
            ? evidenceFrames.map((frame, i) => (
                <button
                  key={`ev-frame-${i}`}
                  type="button"
                  className="absolute top-1/2 z-[7] h-4 w-0.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-white/70 transition-transform hover:scale-y-125 hover:bg-white"
                  title={`证据帧 ${formatClock(frame.time)}`}
                  onMouseEnter={() => setHoverFrameIndex(i)}
                  onMouseLeave={() => setHoverFrameIndex((current) => (current === i ? null : current))}
                  onClick={(e) => {
                    e.stopPropagation();
                    seekTo(frame.time);
                  }}
                  style={{ left: `${pct(frame.time)}%` }}
                />
              ))
            : null}

          {/* Evidence-frame hover thumbnail */}
          {duration > 0 && hoverFrameIndex !== null && evidenceFrames[hoverFrameIndex]?.image_url ? (
            <div
              className="pointer-events-none absolute -top-[88px] z-[8] -translate-x-1/2 overflow-hidden rounded-lg border border-white/20 bg-black/80 shadow-glow"
              style={{ left: `${pct(evidenceFrames[hoverFrameIndex]!.time)}%` }}
            >
              <img
                src={evidenceFrames[hoverFrameIndex]!.image_url}
                alt={`证据帧 ${formatClock(evidenceFrames[hoverFrameIndex]!.time)}`}
                className="h-20 w-auto max-w-[160px] object-cover"
              />
              <span className="block bg-black/70 px-1.5 py-0.5 text-center text-[10px] text-white/90">
                {formatClock(evidenceFrames[hoverFrameIndex]!.time)}
              </span>
            </div>
          ) : null}

          {/* Native range overlay (keyboard accessible scrubber) */}
          <input
            type="range"
            min={0}
            max={duration || 100}
            step={0.05}
            value={currentTime}
            onChange={handleScrubberChange}
            onClick={(e) => e.stopPropagation()}
            aria-label="进度"
            className={`absolute inset-0 h-full w-full cursor-pointer opacity-0 ${
              segmentBarsInteractive ? "pointer-events-none z-[1]" : "z-[4]"
            }`}
          />

          {/* Playhead */}
          <div
            className="pointer-events-none absolute top-1/2 z-[8] h-3.5 w-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-white shadow-lg"
            style={{ left: `${pct(currentTime)}%` }}
          />

          {/* Hover time tooltip */}
          {isHoveringProgress && duration > 0 ? (
            <div
              className="pointer-events-none absolute -top-7 z-[7] -translate-x-1/2 rounded-md bg-black/80 px-2 py-0.5 text-[11px] text-white"
              style={{ left: `${pct(hoverTime)}%` }}
            >
              {formatClock(hoverTime)}
            </div>
          ) : null}
        </div>

        {/* Buttons row */}
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={togglePlay}
              className="grid h-9 w-9 place-items-center rounded-full bg-white/20 text-white transition-colors hover:bg-white/30"
              aria-label={isPlaying ? "暂停" : "播放"}
            >
              {isPlaying ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4 translate-x-0.5" />}
            </button>
            <span className="font-mono text-xs text-white/90 tabular-nums">
              {formatClock(currentTime)} / {formatClock(duration)}
            </span>
            {currentSegment ? (
              <span className="hidden items-center gap-1.5 rounded-full bg-white/15 px-2.5 py-1 text-[11px] text-white/90 sm:inline-flex">
                <span className="h-2 w-2 rounded-full" style={{ backgroundColor: roleColor(currentSegment.role) }} />
                {currentSegment.label || currentSegment.id || "当前片段"}
              </span>
            ) : null}
          </div>

          <div className="flex items-center gap-2">
            <div className="group/volume flex items-center gap-1.5">
              <button
                type="button"
                onClick={toggleMute}
                className="grid h-9 w-9 place-items-center rounded-full text-white transition-colors hover:bg-white/10"
                aria-label={isMuted || volume === 0 ? "取消静音" : "静音"}
              >
                {isMuted || volume === 0 ? <VolumeX className="h-4 w-4" /> : <Volume2 className="h-4 w-4" />}
              </button>
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={isMuted ? 0 : volume}
                onChange={handleVolumeChange}
                aria-label="音量"
                className="h-1 w-0 cursor-pointer accent-accent transition-all duration-200 group-hover/volume:w-16"
              />
            </div>
            <button
              type="button"
              onClick={toggleFullscreen}
              className="grid h-9 w-9 place-items-center rounded-full text-white transition-colors hover:bg-white/10"
              aria-label="全屏"
            >
              <Maximize className="h-4 w-4" />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
