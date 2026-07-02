export function VoiceGridSkeleton() {
  return (
    <div className="grid min-w-0 gap-3 md:grid-cols-2 2xl:grid-cols-3">
      {Array.from({ length: 6 }).map((_, index) => (
        <div className="rounded-[24px] border border-border/80 bg-white/55 p-4" key={index}>
          <div className="h-4 w-24 rounded-full bg-border/60 shimmer" />
          <div className="mt-5 h-7 w-36 rounded-full bg-border/60 shimmer" />
          <div className="mt-6 h-10 rounded-2xl bg-border/60 shimmer" />
        </div>
      ))}
    </div>
  );
}
