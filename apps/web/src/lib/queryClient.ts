import { MutationCache, QueryCache, QueryClient } from "@tanstack/react-query";
import { isApiError } from "../api/client";
import { notifyError } from "../components/ui/Toast";

function shouldToast(error: unknown) {
  return isApiError(error) && error.status !== 401;
}

export function createAppQueryClient() {
  return new QueryClient({
    queryCache: new QueryCache({
      onError: (error) => {
        if (shouldToast(error) && isApiError(error)) {
          notifyError(error);
        }
      },
    }),
    mutationCache: new MutationCache({
      onError: (error) => {
        if (shouldToast(error) && isApiError(error)) {
          notifyError(error);
        }
      },
    }),
    defaultOptions: {
      queries: {
        retry: (failureCount, error) => {
          if (isApiError(error) && [401, 403, 404].includes(error.status)) {
            return false;
          }
          return failureCount < 1;
        },
        refetchOnWindowFocus: false,
      },
    },
  });
}
