import { useCallback, useEffect, useRef, useState } from "react";
import type { CompleteUploadResponse, UploadSession } from "../api/client";
import { useAuth } from "../pages/auth/AuthContext";
import {
  uploadManager,
  type UploadFileInput,
  type UploadManagerStage,
} from "../uploads/manager";

type UploadStage = "idle" | UploadManagerStage;

type UploadState = {
  status: UploadStage;
  progress: number;
  session?: UploadSession;
  result?: CompleteUploadResponse;
  error?: Error;
};

const initialState: UploadState = {
  status: "idle",
  progress: 0,
};

export function useUpload() {
  const { user } = useAuth();
  const [state, setState] = useState<UploadState>(initialState);
  const activeUploadId = useRef<string | null>(null);

  useEffect(() => {
    activeUploadId.current = null;
    setState(initialState);
    if (!user) return undefined;

    return uploadManager.subscribeForUser(user.id, (event) => {
      if (activeUploadId.current === null && event.recovered) {
        activeUploadId.current = event.clientUploadId;
      }
      if (event.clientUploadId !== activeUploadId.current) return;
      setState({
        status: event.status,
        progress: event.progress,
        session: event.session,
        result: event.result,
        error: event.error,
      });
    });
  }, [user?.id]);

  const reset = useCallback(() => {
    activeUploadId.current = null;
    setState(initialState);
  }, []);

  const uploadFile = useCallback(
    async (input: UploadFileInput) => {
      if (!user) throw new Error("请先登录后再上传");
      setState({ status: "preparing", progress: 0 });
      const upload = uploadManager.start(user.id, input);
      activeUploadId.current = upload.clientUploadId;
      return upload.promise;
    },
    [user],
  );

  return {
    ...state,
    reset,
    uploadFile,
  };
}
