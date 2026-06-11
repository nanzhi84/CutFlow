import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, PackagePlus, PlayCircle, Trash2 } from "lucide-react";
import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../../api/client";
import { EmptyState, ErrorState, LoadingState } from "../../components/State";
import { StatusPill } from "../../components/Status";
import { StudioTabs } from "../../components/StudioTabs";
import { useAuth } from "../auth/AuthContext";
import { routes } from "../../routes";
import { TimeText } from "../../components/TimeText";
import { toDisplayUrl } from "../../lib/url";
import { EditorHandoffActions } from "../../components/editor-handoff/EditorHandoffActions";
import { InfiniteScrollSentinel } from "../../components/ui/InfiniteScrollSentinel";

export default function FinishedVideosPage() {
  const { caseId = "" } = useParams();
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [limit, setLimit] = useState(50);
  const caseDetail = useQuery({
    queryKey: ["case", caseId],
    queryFn: () => api.cases.detail(caseId),
    enabled: Boolean(caseId),
  });
  const videos = useQuery({
    queryKey: ["finished-videos", caseId, limit],
    queryFn: () => api.finishedVideos.list(caseId, { limit }),
    enabled: Boolean(caseId),
  });
  const preview = useMutation({
    mutationFn: (id: string) => api.finishedVideos.previewUrl(id),
    onSuccess: (data) => setPreviewUrl(toDisplayUrl(data.url)),
  });
  const download = useMutation({
    mutationFn: (id: string) => api.finishedVideos.downloadUrl(id),
    onSuccess: (data) => {
      const url = toDisplayUrl(data.url);
      if (url) window.open(url, "_blank", "noopener,noreferrer");
    },
  });
  const deleteVideo = useMutation({
    mutationFn: (id: string) => api.finishedVideos.delete(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["finished-videos", caseId] }),
  });
  const createPackage = useMutation({
    mutationFn: (video: { id: string; title: string }) =>
      api.publishing.createPackage({
        source_finished_video_id: video.id,
        title: video.title,
        description: "",
      }),
    onSuccess: () => navigate(routes.casePublish(caseId)),
  });

  const items = videos.data?.items ?? [];
  const hasMore = Boolean(videos.data && items.length >= limit);

  return (
    <section className="pageStack">
      <header className="pageHeader">
        <div>
          <h1>{caseDetail.data?.name ?? "成片"}</h1>
          <p>{items.length} 条成片，可预览、下载和创建发布包。</p>
        </div>
      </header>
      <StudioTabs caseId={caseId} />
      {caseDetail.error ? <ErrorState error={caseDetail.error} /> : null}
      {videos.isLoading ? <LoadingState label="加载成片" /> : null}
      {videos.error ? <ErrorState error={videos.error} /> : null}
      {!videos.isLoading && items.length === 0 ? (
        <EmptyState title="暂无成片" detail="生产成功后成片会显示在这里。" />
      ) : null}

      {previewUrl ? (
        <section className="surface previewPanel">
          <video src={previewUrl} controls />
        </section>
      ) : null}

      {items.length > 0 ? (
        <div className="dataTable surface">
          <div className="tableRow tableHead videoRow">
            <span>标题</span>
            <span>时长</span>
            <span>QC</span>
            <span>创建时间</span>
            <span>动作</span>
          </div>
          {items.map((video) => (
            <div className="tableRow videoRow" key={video.id}>
              <strong>{video.title}</strong>
              <span className="monoNumber">{video.duration_sec.toFixed(1)}s</span>
              <StatusPill status={video.qc_status} />
              <span><TimeText value={video.created_at} /></span>
              <span className="rowActions">
                <button className="ghostButton compactButton" type="button" onClick={() => preview.mutate(video.id)}>
                  <PlayCircle size={14} />
                  <span>预览</span>
                </button>
                <button className="ghostButton compactButton" type="button" onClick={() => download.mutate(video.id)}>
                  <Download size={14} />
                  <span>下载</span>
                </button>
                <button
                  className="ghostButton compactButton"
                  type="button"
                  onClick={() => createPackage.mutate({ id: video.id, title: video.title })}
                >
                  <PackagePlus size={14} />
                  <span>发布包</span>
                </button>
                {isAdmin ? (
                  <button className="ghostButton compactButton dangerButton" type="button" onClick={() => deleteVideo.mutate(video.id)}>
                    <Trash2 size={14} />
                    <span>删除</span>
                  </button>
                ) : null}
                <EditorHandoffActions finishedVideoId={video.id} compact />
              </span>
            </div>
          ))}
          <InfiniteScrollSentinel
            enabled={hasMore && !videos.isFetching}
            onVisible={() => setLimit((current) => current + 50)}
            label="继续加载成片"
          />
        </div>
      ) : null}
    </section>
  );
}
