import { CheckSquare, Loader2, RotateCcw, Search, Send, Square, Users, X } from "lucide-react";
import { useMemo, useState } from "react";
import type {
  PublishAccount,
  PublishAttempt,
  PublishBatch,
  PublishBatchItem,
  PublishClient,
  PublishLoginState,
  PublishPlatform,
} from "../../api/client";
import { TimeText } from "../TimeText";
import { StatusPill } from "../ui/StatusPill";
import { PlatformChips } from "./PlatformChips";
import { PLATFORM_OPTIONS, type PublishDraft, itemCanRetry, platformLabel } from "./publishModel";

const loginStateLabels: Record<PublishLoginState, string> = {
  logged_in: "已登录",
  logged_out: "需重新登录",
  unknown: "待小V猫",
};

const loginStateBadgeClasses: Record<PublishLoginState, string> = {
  logged_in: "bg-status-success/15 text-status-success",
  logged_out: "bg-status-error/15 text-status-error",
  unknown: "bg-black/5 text-text-tertiary",
};

type PublishReviewStepProps = {
  batch: PublishBatch;
  drafts: Record<string, PublishDraft>;
  attempts: PublishAttempt[];
  clients: PublishClient[];
  accounts: PublishAccount[];
  selectedClientId: string;
  selectedAccountIds: string[];
  isSubmitting?: boolean;
  isRetrying?: boolean;
  isAccountsLoading?: boolean;
  onClientChange: (clientId: string) => void;
  onAccountToggle: (account: PublishAccount) => void;
  onDraftChange: (itemId: string, patch: Partial<PublishDraft>) => void;
  onSubmit: () => void;
  onRetry: () => void;
  onBack: () => void;
};

export function PublishReviewStep({
  batch,
  drafts,
  attempts,
  clients,
  accounts,
  selectedClientId,
  selectedAccountIds,
  isSubmitting = false,
  isRetrying = false,
  isAccountsLoading = false,
  onClientChange,
  onAccountToggle,
  onDraftChange,
  onSubmit,
  onRetry,
  onBack,
}: PublishReviewStepProps) {
  const [accountSearch, setAccountSearch] = useState("");
  const [platformFilter, setPlatformFilter] = useState<PublishPlatform | "all">("all");
  const items = batch.items ?? [];
  const draftGroups = useMemo(() => groupItemsByPackage(items), [items]);
  const batchPlatforms = Array.from(new Set(items.map((item) => item.platform))) as PublishPlatform[];
  const batchPlatformSet = useMemo(() => new Set<string>(batchPlatforms), [batchPlatforms]);
  const clientById = useMemo(() => new Map(clients.map((client) => [client.id, client])), [clients]);
  const selectedAccountSet = useMemo(() => new Set(selectedAccountIds), [selectedAccountIds]);
  const selectedAccounts = accounts.filter((account) => selectedAccountSet.has(account.id));
  const selectedPlatformSet = new Set<string>(selectedAccounts.map((account) => account.platform));
  const eligibleGroups = draftGroups.filter((group) => group.items.some((item) => selectedPlatformSet.has(item.platform)));
  const selectedVideoGroups = eligibleGroups.filter((group) => groupIsSelected(group, selectedPlatformSet, drafts));
  const allSelected = eligibleGroups.length > 0 && selectedVideoGroups.length === eligibleGroups.length;
  const publishableCount = selectedVideoGroups.filter((group) => groupIsPublishable(group, selectedPlatformSet, drafts)).length;
  const platformOrder = new Map(PLATFORM_OPTIONS.map((option, index) => [option.value, index]));
  const normalizedSearch = accountSearch.trim().toLowerCase();
  const filteredAccounts = accounts
    .filter((account) => batchPlatformSet.has(account.platform))
    .filter((account) => !selectedClientId || account.client_id === selectedClientId)
    .filter((account) => platformFilter === "all" || account.platform === platformFilter)
    .filter((account) => {
      if (!normalizedSearch) return true;
      const clientName = clientById.get(account.client_id)?.name ?? "";
      return [account.account_name, account.platform_uid, account.xiaovmao_uid, account.id, clientName]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(normalizedSearch));
    })
    .sort((a, b) => {
      const platformCompare = (platformOrder.get(a.platform) ?? 99) - (platformOrder.get(b.platform) ?? 99);
      if (platformCompare !== 0) return platformCompare;
      const clientCompare = (clientById.get(a.client_id)?.name ?? "").localeCompare(clientById.get(b.client_id)?.name ?? "");
      if (clientCompare !== 0) return clientCompare;
      return a.account_name.localeCompare(b.account_name);
    });
  const platformCounts = PLATFORM_OPTIONS.filter((option) => batchPlatformSet.has(option.value)).map((option) => ({
    ...option,
    count: accounts.filter(
      (account) =>
        account.platform === option.value &&
        batchPlatformSet.has(account.platform) &&
        (!selectedClientId || account.client_id === selectedClientId),
    ).length,
  }));
  const canSubmit = publishableCount > 0 && selectedAccounts.length > 0 && !isAccountsLoading;

  function toggleAll() {
    eligibleGroups.forEach((group) => toggleGroup(group, !allSelected));
  }

  function toggleGroup(group: DraftGroup, selected: boolean) {
    group.items
      .filter((item) => selectedPlatformSet.has(item.platform))
      .forEach((item) => onDraftChange(item.id, { selected }));
  }

  return (
    <section className="grid gap-4">
      <div className="card grid gap-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold text-text-primary">选择账号并发布</h2>
            <p className="mt-1 text-sm text-text-secondary">
              已选中 {selectedVideoGroups.length} 条视频、{selectedAccountIds.length} 个账号，可提交 {publishableCount} 条。
            </p>
          </div>
          <StatusPill status={batch.status} />
        </div>
        <div className="rounded-2xl border border-status-info/25 bg-status-info/10 p-4 text-sm leading-6 text-status-info">
          发布账号会先保存到当前案例；自动发布会按草稿平台匹配已选账号并调用小V猫任务。
        </div>

        <div className="grid gap-4 rounded-2xl border border-border/80 bg-white/55 p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h3 className="inline-flex items-center gap-2 text-base font-semibold text-text-primary">
                <Users className="h-4 w-4" />
                发布账号
              </h3>
              <p className="mt-1 text-sm text-text-secondary">按客户、平台和关键词缩小范围，再从账号列表中勾选。</p>
            </div>
            <span className="rounded-full bg-accent/15 px-3 py-1 text-sm font-semibold text-accent">已选 {selectedAccountIds.length} 个</span>
          </div>

          <div className="grid gap-3 lg:grid-cols-[240px_minmax(0,1fr)]">
            <label>
              <span>客户筛选</span>
              <select value={selectedClientId} onChange={(event) => onClientChange(event.target.value)}>
                <option value="">全部客户</option>
                {clients.map((client) => (
                  <option key={client.id} value={client.id}>
                    {client.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>搜索账号</span>
              <span className="relative block">
                <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-text-tertiary" />
                <input
                  value={accountSearch}
                  placeholder="账号名、UID、客户名"
                  onChange={(event) => setAccountSearch(event.target.value)}
                  className="pl-10 pr-10"
                />
                {accountSearch ? (
                  <button
                    type="button"
                    className="absolute right-3 top-1/2 -translate-y-1/2 text-text-tertiary hover:text-text-primary"
                    onClick={() => setAccountSearch("")}
                  >
                    <X className="h-4 w-4" />
                  </button>
                ) : null}
              </span>
            </label>
          </div>

          <div className="grid gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-semibold text-text-primary">可发布平台</span>
              <PlatformChips value={batchPlatforms} compact />
            </div>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                className={`rounded-full border px-3 py-1.5 text-sm transition ${
                  platformFilter === "all"
                    ? "border-accent/35 bg-accent/15 text-accent"
                    : "border-border/75 bg-white/70 text-text-secondary hover:border-accent/25"
                }`}
                onClick={() => setPlatformFilter("all")}
              >
                全部平台
              </button>
              {platformCounts.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  className={`rounded-full border px-3 py-1.5 text-sm transition ${
                    platformFilter === option.value
                      ? "border-accent/35 bg-accent/15 text-accent"
                      : "border-border/75 bg-white/70 text-text-secondary hover:border-accent/25"
                  }`}
                  onClick={() => setPlatformFilter(option.value)}
                >
                  {option.label} <span className="text-text-tertiary">{option.count}</span>
                </button>
              ))}
            </div>
          </div>

          {selectedAccounts.length > 0 ? (
            <div className="rounded-xl border border-border/70 bg-white/65 p-3">
              <div className="mb-2 text-xs font-semibold text-text-tertiary">已选账号</div>
              <div className="flex max-h-24 flex-wrap gap-2 overflow-y-auto pr-1 text-xs text-text-secondary">
                {selectedAccounts.map((account) => (
                  <button
                    key={account.id}
                    type="button"
                    className="inline-flex items-center gap-1 rounded-full bg-accent/10 px-2.5 py-1 text-accent"
                    onClick={() => onAccountToggle(account)}
                  >
                    {platformLabel(account.platform)} · {clientById.get(account.client_id)?.name ?? "未知客户"} · {account.account_name}
                    <X className="h-3 w-3" />
                  </button>
                ))}
              </div>
            </div>
          ) : null}

          {isAccountsLoading ? (
            <div className="rounded-2xl border border-dashed border-border/80 bg-white/50 p-6 text-center text-sm text-text-secondary">
              正在加载发布账号...
            </div>
          ) : (
            <div className="overflow-hidden rounded-2xl border border-border/80 bg-white/65">
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/70 px-3 py-2 text-sm">
                <span className="font-semibold text-text-primary">账号列表</span>
                <span className="text-text-tertiary">显示 {filteredAccounts.length} 个</span>
              </div>
              <div className="max-h-[420px] overflow-y-auto">
                {filteredAccounts.map((account) => {
                  const checked = selectedAccountSet.has(account.id);
                  return (
                    <label
                      key={account.id}
                      className={`grid cursor-pointer grid-cols-[auto_72px_minmax(0,1fr)_auto] items-center gap-3 border-b border-border/60 px-3 py-2.5 transition last:border-b-0 ${
                        checked ? "bg-accent/10" : "hover:bg-surface-hover/45"
                      }`}
                    >
                      <input
                        type="checkbox"
                        className="h-4 w-4"
                        checked={checked}
                        onChange={() => onAccountToggle(account)}
                      />
                      <span className="rounded-full bg-surface-hover px-2.5 py-1 text-center text-xs font-semibold text-text-secondary">
                        {platformLabel(account.platform)}
                      </span>
                      <span className="min-w-0">
                        <span className="block truncate text-sm font-semibold text-text-primary">{account.account_name}</span>
                        <span className="mt-0.5 block truncate text-xs text-text-tertiary">
                          {clientById.get(account.client_id)?.name ?? "未知客户"} · {account.platform_uid ?? account.xiaovmao_uid ?? account.id.slice(0, 8)}
                        </span>
                      </span>
                      <span className={`rounded-full px-2 py-0.5 text-xs ${loginStateBadgeClasses[account.login_state]}`}>
                        {loginStateLabels[account.login_state]}
                      </span>
                    </label>
                  );
                })}
                {filteredAccounts.length === 0 ? (
                  <div className="p-6 text-center text-sm text-text-secondary">没有匹配当前筛选条件的账号。</div>
                ) : null}
              </div>
            </div>
          )}
        </div>
        <div className="flex flex-wrap justify-between gap-3 border-t border-border/70 pt-4">
          <button className="btn-secondary" type="button" onClick={onBack}>
            返回编辑
          </button>
          <div className="flex flex-wrap gap-2">
            <button className="btn-secondary" type="button" onClick={toggleAll}>
              {allSelected ? <CheckSquare className="h-4 w-4" /> : <Square className="h-4 w-4" />}
              全选
            </button>
            <button className="btn-primary" type="button" disabled={isSubmitting || !canSubmit} onClick={onSubmit}>
              {isSubmitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
              自动发布
            </button>
          </div>
        </div>
      </div>

      <div className="card grid gap-3">
        <div>
          <h3 className="text-base font-semibold text-text-primary">待发布视频</h3>
          <p className="mt-1 text-sm text-text-secondary">一条视频只显示一次；会投递到已选账号所在的平台。</p>
        </div>
        {draftGroups.map((group) => {
          const item = group.items[0];
          const draft = drafts[item.id];
          const groupSelected = groupIsSelected(group, selectedPlatformSet, drafts);
          const publishable = groupIsPublishable(group, selectedPlatformSet, drafts);
          const targetAccounts = selectedAccounts.filter((account) => group.platforms.includes(account.platform));
          const retryItem = group.items.find(itemCanRetry);
          return (
            <div
              key={group.packageId}
              className={`rounded-2xl border p-4 ${
                groupSelected && targetAccounts.length > 0 ? "border-border/80 bg-white/60" : "border-border/60 bg-surface-hover/35 opacity-75"
              }`}
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <label className="flex min-w-0 cursor-pointer items-start gap-3">
                  <input
                    type="checkbox"
                    className="mt-0.5 h-4 w-4"
                    checked={groupSelected}
                    disabled={targetAccounts.length === 0}
                    onChange={(event) => toggleGroup(group, event.target.checked)}
                  />
                  <span className="min-w-0">
                    <span className="block truncate text-sm font-semibold text-text-primary">{draft?.title || item.title}</span>
                    <span className="mt-1 block truncate text-xs font-normal text-text-secondary">
                      {draft?.description || item.description || "无正文"}
                    </span>
                  </span>
                </label>
                <div className="flex max-w-full flex-wrap items-center justify-end gap-2">
                  {targetAccounts.length > 0 ? (
                    targetAccounts.map((account) => (
                      <span key={account.id} className="rounded-full bg-accent/10 px-2.5 py-1 text-xs text-accent">
                        {platformLabel(account.platform)} · {account.account_name}
                      </span>
                    ))
                  ) : (
                    <span className="badge bg-surface-hover text-text-tertiary">未选账号</span>
                  )}
                </div>
              </div>
              {!publishable && groupSelected ? (
                <p className="mt-3 text-xs text-status-error">当前视频没有可提交的平台条目，请检查账号或条目状态。</p>
              ) : null}
              {retryItem ? (
                <button className="btn-secondary mt-3 min-h-9 px-3" type="button" disabled={isRetrying} onClick={onRetry}>
                  {isRetrying ? <Loader2 className="h-4 w-4 animate-spin" /> : <RotateCcw className="h-4 w-4" />}
                  重新自动发布
                </button>
              ) : null}
            </div>
          );
        })}
      </div>

      <div className="card grid gap-3">
        <h3 className="text-base font-semibold text-text-primary">发布结果</h3>
        {attempts.map((attempt) => (
          <div key={attempt.id} className="rounded-2xl border border-border/80 bg-white/60 p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="min-w-0">
                <p className="font-mono text-xs text-text-tertiary">{attempt.id}</p>
                <p className="mt-1 text-sm text-text-secondary">
                  {attempt.platforms.map(platformLabel).join(" / ")} · {attempt.manual_review ? "待人工记录" : "自动发布"} · {attempt.adapter_id ?? "未记录适配器"}
                </p>
              </div>
              <StatusPill status={attempt.status} />
            </div>
            <div className="mt-2 flex flex-wrap gap-3 text-xs text-text-tertiary">
              <span>创建 <TimeText value={attempt.created_at} /></span>
              {attempt.finished_at ? <span>完成 <TimeText value={attempt.finished_at} /></span> : null}
              {attempt.external_task_id ? <span>任务 {attempt.external_task_id}</span> : null}
            </div>
            {attempt.error ? <p className="mt-2 text-sm text-status-error">{attempt.error.message}</p> : null}
            <AttemptStatusList attempt={attempt} />
          </div>
        ))}
        {attempts.length === 0 ? (
          <div className="rounded-2xl border border-dashed border-border/80 bg-white/50 p-6 text-center text-sm text-text-secondary">
            尚无发布尝试；提交后会显示状态和时间。
          </div>
        ) : null}
      </div>
    </section>
  );
}

function AttemptStatusList({ attempt }: { attempt: PublishAttempt }) {
  const results = (attempt.results ?? [])
    .map((item) => (item && typeof item === "object" && !Array.isArray(item) ? (item as Record<string, unknown>) : null))
    .filter((item): item is Record<string, unknown> => Boolean(item))
    .filter((item) => "platform" in item || "account" in item || "external_task_id" in item || "xiaovmao_status_label" in item || "error" in item || "url" in item);

  if (results.length === 0) return null;

  return (
    <div className="mt-3 grid gap-2">
      {results.map((result, index) => {
        const success = result.success === true;
        const failed = result.success === false || Boolean(result.error);
        const platform = typeof result.platform === "string" ? result.platform : null;
        const account = typeof result.account === "string" ? result.account : null;
        const statusLabel =
          textValue(result.xiaovmao_status_label) ??
          (result.scheduled === true ? "已进入定时队列" : success ? "已发布" : failed ? "失败" : "已提交");
        const taskId = textValue(result.external_task_id);
        const error = textValue(result.error);
        const url = textValue(result.url);

        return (
          <div key={`${taskId ?? index}`} className="rounded-xl border border-border/70 bg-white/55 px-3 py-2 text-sm">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-semibold text-text-primary">
                {platform ? platformLabel(platform) : "发布任务"}
                {account ? <span className="ml-2 font-mono text-xs font-normal text-text-tertiary">{account}</span> : null}
              </span>
              <span
                className={`rounded-full px-2.5 py-1 text-xs font-semibold ${
                  failed
                    ? "bg-status-error/15 text-status-error"
                    : success
                      ? "bg-status-success/15 text-status-success"
                      : "bg-status-info/15 text-status-info"
                }`}
              >
                {statusLabel}
              </span>
            </div>
            <div className="mt-1 flex flex-wrap gap-3 text-xs text-text-tertiary">
              {taskId ? <span>小V猫任务 {taskId}</span> : null}
              {typeof result.xiaovmao_status === "number" ? <span>状态码 {result.xiaovmao_status}</span> : null}
            </div>
            {error ? <p className="mt-1 text-xs leading-5 text-status-error">{error}</p> : null}
            {url ? (
              <a className="mt-1 inline-flex text-xs font-semibold text-accent hover:underline" href={url} target="_blank" rel="noreferrer">
                查看发布链接
              </a>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function textValue(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return null;
}

type DraftGroup = {
  packageId: string;
  items: PublishBatchItem[];
  platforms: string[];
};

function groupItemsByPackage(items: PublishBatchItem[]): DraftGroup[] {
  const groups = new Map<string, DraftGroup>();
  items.forEach((item) => {
    const packageId = item.publish_package_id;
    const current = groups.get(packageId);
    if (current) {
      current.items.push(item);
      if (!current.platforms.includes(item.platform)) current.platforms.push(item.platform);
      return;
    }
    groups.set(packageId, {
      packageId,
      items: [item],
      platforms: [item.platform],
    });
  });
  return Array.from(groups.values());
}

function groupIsSelected(
  group: DraftGroup,
  selectedPlatformSet: Set<string>,
  drafts: Record<string, PublishDraft>,
): boolean {
  return group.items.some((item) => selectedPlatformSet.has(item.platform) && (drafts[item.id]?.selected ?? item.selected));
}

function groupIsPublishable(
  group: DraftGroup,
  selectedPlatformSet: Set<string>,
  drafts: Record<string, PublishDraft>,
): boolean {
  return group.items.some(
    (item) =>
      selectedPlatformSet.has(item.platform) &&
      (drafts[item.id]?.selected ?? item.selected) &&
      !["published", "publishing", "excluded"].includes(item.status),
  );
}
