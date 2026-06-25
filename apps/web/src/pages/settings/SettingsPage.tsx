import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, FlaskConical, KeyRound, Plus, RotateCw, ShieldAlert, ToggleLeft, ToggleRight } from "lucide-react";
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, createIdempotencyKey, type ProviderProfile, type SecretPreview } from "../../api/client";
import { EmptyState, ErrorState, LoadingState } from "../../components/ui/State";
import { Modal } from "../../components/ui/Modal";
import { StatusPill } from "../../components/ui/StatusPill";
import { useAuth } from "../auth/AuthContext";
import { labelForStatus } from "../../lib/status";

type SettingsTab = "providers" | "secrets" | "prices" | "cookies";

type ProfileForm = {
  id?: string;
  display_name: string;
  provider_id: string;
  model_id: string;
  capability: string;
  environment: "local" | "dev" | "staging" | "prod";
  secret_ref: string;
  default_options: string;
};

type SecretForm = {
  provider_id: string;
  environment: "local" | "dev" | "staging" | "prod";
  name: string;
  plaintext_secret: string;
};

type PriceForm = {
  provider_id: string;
  model_id: string;
  capability_id: string;
  unit: "input_token" | "output_token" | "media_second" | "call";
  amount: string;
  currency: string;
};

const emptyProfile: ProfileForm = {
  display_name: "",
  provider_id: "",
  model_id: "",
  capability: "text.generate",
  environment: "prod",
  secret_ref: "",
  default_options: "{}",
};

const emptySecret: SecretForm = {
  provider_id: "",
  environment: "prod",
  name: "API key",
  plaintext_secret: "",
};

const emptyPrice: PriceForm = {
  provider_id: "",
  model_id: "",
  capability_id: "text.generate",
  unit: "call",
  amount: "0.5",
  currency: "CNY",
};

function parseOptions(value: string) {
  const parsed = JSON.parse(value || "{}") as Record<string, unknown>;
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("default_options 必须是 JSON object");
  }
  return parsed;
}

function tabFromSearch(value: string | null): SettingsTab {
  return value === "secrets" || value === "prices" || value === "cookies" ? value : "providers";
}

function tabLabel(tab: SettingsTab) {
  if (tab === "providers") return "供应商";
  if (tab === "secrets") return "密钥";
  if (tab === "prices") return "价格";
  return "对标 Cookie";
}

function environmentLabel(value?: string | null) {
  if (value === "local") return "本地";
  if (value === "dev") return "开发";
  if (value === "staging") return "预发";
  if (value === "prod") return "生产";
  return "未知环境";
}

function unitLabel(value?: string | null) {
  if (value === "input_token") return "输入 Token";
  if (value === "output_token") return "输出 Token";
  if (value === "media_second") return "媒体秒";
  if (value === "call") return "调用";
  return "未知单位";
}

function ReadOnlyNotice({ isAdmin }: { isAdmin: boolean }) {
  if (isAdmin) return null;
  return (
    <div className="stateBox">
      <ShieldAlert size={16} />
      <span>当前账号不是管理员，设置页只读，写操作已隐藏。</span>
    </div>
  );
}

export default function SettingsPage() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = tabFromSearch(searchParams.get("tab"));
  const queryClient = useQueryClient();
  const [profileForm, setProfileForm] = useState<ProfileForm>(emptyProfile);
  const [profileError, setProfileError] = useState<unknown>(null);
  const [secretForm, setSecretForm] = useState<SecretForm>(emptySecret);
  const [secretError, setSecretError] = useState<unknown>(null);
  const [secretOp, setSecretOp] = useState<{ mode: "rotate" | "disable"; secret: SecretPreview } | null>(null);
  const [secretPlaintext, setSecretPlaintext] = useState("");
  const [secretReason, setSecretReason] = useState("");
  const [priceForm, setPriceForm] = useState<PriceForm>(emptyPrice);
  const [priceError, setPriceError] = useState<unknown>(null);
  const [selectedCatalogId, setSelectedCatalogId] = useState<string | null>(null);
  const [governReason, setGovernReason] = useState("");
  const [healthByProfile, setHealthByProfile] = useState<Record<string, string>>({});

  const profiles = useQuery({ queryKey: ["provider-profiles"], queryFn: () => api.providers.profiles({ limit: 200 }) });
  const capabilities = useQuery({ queryKey: ["provider-capabilities"], queryFn: api.providers.capabilities });
  const secrets = useQuery({
    queryKey: ["secrets"],
    queryFn: api.secrets.list,
    enabled: isAdmin,
  });
  const catalogs = useQuery({
    queryKey: ["price-catalogs"],
    queryFn: () => api.providers.priceCatalogs({ limit: 100 }),
  });
  const selectedCatalog = selectedCatalogId ?? catalogs.data?.items[0]?.id ?? null;
  const catalogItems = useQuery({
    queryKey: ["price-catalog-items", selectedCatalog],
    queryFn: () => api.providers.priceCatalogItems(selectedCatalog!, { limit: 200 }),
    enabled: Boolean(selectedCatalog),
  });

  const capabilityOptions = useMemo(() => capabilities.data ?? [], [capabilities.data]);
  const secretOptions = secrets.data?.items ?? [];

  const saveProfile = useMutation({
    mutationFn: () => {
      const defaultOptions = parseOptions(profileForm.default_options);
      if (profileForm.id) {
        return api.providers.patchProfile(profileForm.id, {
          display_name: profileForm.display_name,
          secret_ref: profileForm.secret_ref || null,
          default_options: defaultOptions,
          concurrency_key: "default",
          timeout_sec: 30,
        });
      }
      return api.providers.createProfile({
        provider_id: profileForm.provider_id,
        model_id: profileForm.model_id,
        capability: profileForm.capability,
        display_name: profileForm.display_name,
        environment: profileForm.environment,
        secret_ref: profileForm.secret_ref || null,
        concurrency_key: "default",
        timeout_sec: 30,
        options_schema_ref: {
          schema_id: `${profileForm.capability}.options`,
          schema_version: "v1",
          dialect: "pydantic",
          sha256: "dev-unpinned",
        },
        default_options: defaultOptions,
        version: "v1",
      });
    },
    onSuccess: async () => {
      setProfileForm(emptyProfile);
      await queryClient.invalidateQueries({ queryKey: ["provider-profiles"] });
    },
    onError: setProfileError,
  });

  const patchProfile = useMutation({
    mutationFn: ({ profile, enabled }: { profile: ProviderProfile; enabled: boolean }) =>
      api.providers.patchProfile(profile.id, { enabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["provider-profiles"] }),
  });

  const testProfile = useMutation({
    mutationFn: (profile: ProviderProfile) => api.providers.testProfile(profile.id, { sample_input: {} }),
    onSuccess: (result) => {
      setHealthByProfile((current) => ({
        ...current,
        [result.profile_id]: result.ok ? `通过，${result.latency_ms ?? "-"}ms` : result.error?.message ?? "测试失败",
      }));
    },
  });

  const createSecret = useMutation({
    mutationFn: () => api.secrets.create(secretForm),
    onSuccess: async () => {
      setSecretForm(emptySecret);
      await queryClient.invalidateQueries({ queryKey: ["secrets"] });
    },
    onError: setSecretError,
  });

  const rotateSecret = useMutation({
    mutationFn: () =>
      api.secrets.rotate(secretOp!.secret.id, {
        plaintext_secret: secretPlaintext,
        reason: secretReason,
      }),
    onSuccess: async () => {
      setSecretOp(null);
      setSecretPlaintext("");
      setSecretReason("");
      await queryClient.invalidateQueries({ queryKey: ["secrets"] });
    },
  });

  const disableSecret = useMutation({
    mutationFn: () => api.secrets.disable(secretOp!.secret.id, { reason: secretReason }),
    onSuccess: async () => {
      setSecretOp(null);
      setSecretReason("");
      await queryClient.invalidateQueries({ queryKey: ["secrets"] });
    },
  });

  const createPriceCatalog = useMutation({
    mutationFn: () => {
      const catalogId = createIdempotencyKey("price_catalog").replaceAll("-", "_");
      const itemId = createIdempotencyKey("price_item").replaceAll("-", "_");
      return api.providers.upsertPriceCatalog({
        catalog: {
          id: catalogId,
          version: 1,
          schema_version: "v1",
          provider_id: priceForm.provider_id,
          status: "draft",
          currency: priceForm.currency,
        },
        items: [
          {
            id: itemId,
            version: 1,
            schema_version: "v1",
            catalog_id: catalogId,
            provider_id: priceForm.provider_id,
            model_id: priceForm.model_id,
            capability_id: priceForm.capability_id,
            unit: priceForm.unit,
            unit_price: { amount: Number(priceForm.amount), currency: priceForm.currency },
          },
        ],
      });
    },
    onSuccess: async (catalog) => {
      setSelectedCatalogId(catalog.id);
      await queryClient.invalidateQueries({ queryKey: ["price-catalogs"] });
      await queryClient.invalidateQueries({ queryKey: ["price-catalog-items"] });
    },
    onError: setPriceError,
  });

  const approveCatalog = useMutation({
    mutationFn: (catalogId: string) => api.providers.approvePriceCatalog(catalogId, { reason: governReason }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["price-catalogs"] }),
  });

  const [cookieText, setCookieText] = useState("");
  const [cookieFormat, setCookieFormat] = useState<"auto" | "header" | "netscape" | "json">("auto");
  const [cookieTestUrl, setCookieTestUrl] = useState("");
  const [cookieError, setCookieError] = useState<unknown>(null);

  const cookieStatus = useQuery({
    queryKey: ["reference-extractor-status"],
    queryFn: api.creative.referenceExtractorStatus,
    enabled: tab === "cookies",
  });
  const importCookies = useMutation({
    mutationFn: () =>
      api.creative.importReferenceCookies({ cookie_text: cookieText, format: cookieFormat, source: "manual" }),
    onSuccess: async () => {
      setCookieText("");
      setCookieError(null);
      await queryClient.invalidateQueries({ queryKey: ["reference-extractor-status"] });
    },
    onError: setCookieError,
  });
  const testCookies = useMutation({
    mutationFn: () => api.creative.testReferenceCookies({ url: cookieTestUrl.trim() || null }),
    onError: setCookieError,
  });
  const publishCatalog = useMutation({
    mutationFn: (catalogId: string) => api.providers.publishPriceCatalog(catalogId, { reason: governReason }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["price-catalogs"] }),
  });

  return (
    <section className="pageStack">
      <header className="pageHeader">
        <div>
          <h1>设置</h1>
          <p>供应商能力、密钥和价格表配置。</p>
        </div>
      </header>
      <nav className="tabs" aria-label="设置 tabs">
        {(["providers", "secrets", "prices", "cookies"] as SettingsTab[]).map((item) => (
          <button
            className={`tabLink ${tab === item ? "active" : ""}`}
            type="button"
            onClick={() => setSearchParams({ tab: item })}
            key={item}
          >
            {tabLabel(item)}
          </button>
        ))}
      </nav>
      <ReadOnlyNotice isAdmin={isAdmin} />

      {tab === "providers" ? (
        <div className="settingsGrid">
          <section className="surface formSection">
            <div className="sectionHeader">
              <h2>供应商配置</h2>
              {profiles.isLoading ? <span>加载中</span> : null}
            </div>
            {profiles.error ? <ErrorState error={profiles.error} /> : null}
            <div className="listTable">
              {(profiles.data?.items ?? []).map((profile) => (
                <div className="listRow" key={profile.id}>
                  <div>
                    <strong>{profile.display_name}</strong>
                    <span>{profile.provider_id} / {profile.model_id} / {profile.capability}</span>
                    <span>{environmentLabel(profile.environment)} · {profile.secret_ref ? "密钥已绑定" : "未绑定密钥"}</span>
                    {healthByProfile[profile.id] ? <span>{healthByProfile[profile.id]}</span> : null}
                  </div>
                  <div className="rowActions">
                    {isAdmin ? (
                      <>
                        <button className="ghostButton compactButton" type="button" onClick={() => setProfileForm({
                          id: profile.id,
                          display_name: profile.display_name,
                          provider_id: profile.provider_id,
                          model_id: profile.model_id,
                          capability: profile.capability,
                          environment: profile.environment,
                          secret_ref: profile.secret_ref ?? "",
                          default_options: JSON.stringify(profile.default_options ?? {}, null, 2),
                        })}>
                          编辑
                        </button>
                        <button className="ghostButton compactButton" type="button" onClick={() => testProfile.mutate(profile)}>
                          <FlaskConical size={14} />
                          <span>测试</span>
                        </button>
                        <button className="ghostButton compactButton" type="button" onClick={() => patchProfile.mutate({ profile, enabled: !profile.enabled })}>
                          {profile.enabled ? <ToggleRight size={15} /> : <ToggleLeft size={15} />}
                          <span>{profile.enabled ? "禁用" : "启用"}</span>
                        </button>
                      </>
                    ) : null}
                  </div>
                </div>
              ))}
            </div>
            {!profiles.isLoading && !profiles.data?.items.length ? <EmptyState title="暂无供应商配置" /> : null}
          </section>

          {isAdmin ? (
            <form
              className="surface formSection"
              onSubmit={(event) => {
                event.preventDefault();
                setProfileError(null);
                saveProfile.mutate();
              }}
            >
              <h2>{profileForm.id ? "编辑配置" : "新建配置"}</h2>
              <label><span>显示名</span><input required value={profileForm.display_name} onChange={(event) => setProfileForm((current) => ({ ...current, display_name: event.target.value }))} /></label>
              <div className="twoCol">
                <label><span>供应商 ID</span><input value={profileForm.provider_id} onChange={(event) => setProfileForm((current) => ({ ...current, provider_id: event.target.value }))} /></label>
                <label><span>模型 ID</span><input value={profileForm.model_id} onChange={(event) => setProfileForm((current) => ({ ...current, model_id: event.target.value }))} /></label>
              </div>
              <div className="twoCol">
                <label>
                  <span>能力</span>
                  <select value={profileForm.capability} onChange={(event) => setProfileForm((current) => ({ ...current, capability: event.target.value }))}>
                    <option value={profileForm.capability}>{profileForm.capability}</option>
                    {capabilityOptions.map((capability) => (
                      <option value={capability.capability} key={capability.id}>{capability.display_name}</option>
                    ))}
                  </select>
                </label>
                <label>
                  <span>环境</span>
                  <select value={profileForm.environment} onChange={(event) => setProfileForm((current) => ({ ...current, environment: event.target.value as ProfileForm["environment"] }))}>
                    <option value="local">本地</option>
                    <option value="dev">开发</option>
                    <option value="staging">预发</option>
                    <option value="prod">生产</option>
                  </select>
                </label>
              </div>
              <label>
                <span>绑定密钥</span>
                <select value={profileForm.secret_ref} onChange={(event) => setProfileForm((current) => ({ ...current, secret_ref: event.target.value }))}>
                  <option value="">不绑定</option>
                  {secretOptions.map((secret) => (
                    <option value={secret.secret_ref ?? ""} key={secret.id}>{secret.provider_id}/{secret.environment}/{secret.name}</option>
                  ))}
                </select>
              </label>
              <label><span>默认选项 JSON</span><textarea value={profileForm.default_options} onChange={(event) => setProfileForm((current) => ({ ...current, default_options: event.target.value }))} /></label>
              {profileError ? <ErrorState error={profileError} /> : null}
              <div className="formActions">
                <button className="ghostButton" type="button" onClick={() => setProfileForm(emptyProfile)}>清空</button>
                <button className="primaryButton" type="submit" disabled={saveProfile.isPending}><Plus size={16} /><span>保存</span></button>
              </div>
            </form>
          ) : null}
        </div>
      ) : null}

      {tab === "secrets" ? (
        <div className="settingsGrid">
          <section className="surface formSection">
            <h2>密钥</h2>
            {!isAdmin ? <EmptyState title="密钥仅管理员可见" /> : null}
            {secrets.isLoading ? <LoadingState /> : null}
            {secrets.error ? <ErrorState error={secrets.error} /> : null}
            {(secrets.data?.items ?? []).map((secret) => (
              <div className="listRow" key={secret.id}>
                <div>
                  <strong>{secret.provider_id} / {secret.name}</strong>
                  <span>{environmentLabel(secret.environment)} · {secret.masked_value}</span>
                  <span>{labelForStatus(secret.status)}</span>
                </div>
                {isAdmin ? (
                  <div className="rowActions">
                    <button className="ghostButton compactButton" type="button" onClick={() => setSecretOp({ mode: "rotate", secret })}><RotateCw size={14} /><span>轮换</span></button>
                    <button className="ghostButton compactButton dangerButton" type="button" onClick={() => setSecretOp({ mode: "disable", secret })}>禁用</button>
                  </div>
                ) : null}
              </div>
            ))}
          </section>
          {isAdmin ? (
            <form
              className="surface formSection"
              onSubmit={(event) => {
                event.preventDefault();
                setSecretError(null);
                createSecret.mutate();
              }}
            >
              <h2>新建密钥</h2>
              <div className="twoCol">
                <label><span>供应商 ID</span><input value={secretForm.provider_id} onChange={(event) => setSecretForm((current) => ({ ...current, provider_id: event.target.value }))} /></label>
                <label><span>名称</span><input value={secretForm.name} onChange={(event) => setSecretForm((current) => ({ ...current, name: event.target.value }))} /></label>
              </div>
              <label>
                <span>环境</span>
                <select value={secretForm.environment} onChange={(event) => setSecretForm((current) => ({ ...current, environment: event.target.value as SecretForm["environment"] }))}>
                  <option value="local">本地</option>
                  <option value="dev">开发</option>
                  <option value="staging">预发</option>
                  <option value="prod">生产</option>
                </select>
              </label>
              <label><span>明文（仅提交一次）</span><input type="password" value={secretForm.plaintext_secret} onChange={(event) => setSecretForm((current) => ({ ...current, plaintext_secret: event.target.value }))} required /></label>
              {secretError ? <ErrorState error={secretError} /> : null}
              <button className="primaryButton" type="submit" disabled={createSecret.isPending}><KeyRound size={16} /><span>创建密钥</span></button>
            </form>
          ) : null}
        </div>
      ) : null}

      {tab === "prices" ? (
        <div className="settingsGrid">
          <section className="surface formSection">
            <h2>价格表</h2>
            {catalogs.isLoading ? <LoadingState /> : null}
            {catalogs.error ? <ErrorState error={catalogs.error} /> : null}
            {(catalogs.data?.items ?? []).map((catalog) => (
              <button className={`listRow rowButton ${selectedCatalog === catalog.id ? "selected" : ""}`} type="button" key={catalog.id} onClick={() => setSelectedCatalogId(catalog.id)}>
                <div>
                  <strong>{catalog.provider_id}</strong>
                  <span>{catalog.id}</span>
                  <span>{catalog.currency} · {labelForStatus(catalog.status)}</span>
                </div>
                <StatusPill status={catalog.status} />
              </button>
            ))}
            {!catalogs.isLoading && !catalogs.data?.items.length ? <EmptyState title="暂无价格表" /> : null}
          </section>
          <section className="surface formSection">
            <h2>价格项</h2>
            {catalogItems.isLoading ? <LoadingState /> : null}
            {catalogItems.error ? <ErrorState error={catalogItems.error} /> : null}
            {(catalogItems.data?.items ?? []).map((item) => (
              <div className="listRow" key={item.id}>
                <div>
                  <strong>{item.model_id} / {item.capability_id}</strong>
                  <span>{unitLabel(item.unit)}</span>
                </div>
                <span className="monoNumber">{item.unit_price.amount} {item.unit_price.currency}</span>
              </div>
            ))}
            {isAdmin && selectedCatalog ? (
              <div className="governActions">
                <input placeholder="审批/发布原因" value={governReason} onChange={(event) => setGovernReason(event.target.value)} />
                <button className="ghostButton" type="button" disabled={!governReason} onClick={() => approveCatalog.mutate(selectedCatalog)}><CheckCircle2 size={15} /><span>审批</span></button>
                <button className="primaryButton" type="button" disabled={!governReason} onClick={() => publishCatalog.mutate(selectedCatalog)}><CheckCircle2 size={15} /><span>发布</span></button>
              </div>
            ) : null}
          </section>
          {isAdmin ? (
            <form
              className="surface formSection"
              onSubmit={(event) => {
                event.preventDefault();
                setPriceError(null);
                createPriceCatalog.mutate();
              }}
            >
              <h2>新建价格表</h2>
              <div className="twoCol">
                <label><span>供应商 ID</span><input value={priceForm.provider_id} onChange={(event) => setPriceForm((current) => ({ ...current, provider_id: event.target.value }))} /></label>
                <label><span>模型 ID</span><input value={priceForm.model_id} onChange={(event) => setPriceForm((current) => ({ ...current, model_id: event.target.value }))} /></label>
              </div>
              <label><span>能力 ID</span><input value={priceForm.capability_id} onChange={(event) => setPriceForm((current) => ({ ...current, capability_id: event.target.value }))} /></label>
              <div className="twoCol">
                <label>
                  <span>计价单位</span>
                  <select value={priceForm.unit} onChange={(event) => setPriceForm((current) => ({ ...current, unit: event.target.value as PriceForm["unit"] }))}>
                    <option value="call">调用</option>
                    <option value="input_token">输入 Token</option>
                    <option value="output_token">输出 Token</option>
                    <option value="media_second">媒体秒</option>
                  </select>
                </label>
                <label><span>单价</span><input value={priceForm.amount} onChange={(event) => setPriceForm((current) => ({ ...current, amount: event.target.value }))} /></label>
              </div>
              <label><span>币种</span><input value={priceForm.currency} onChange={(event) => setPriceForm((current) => ({ ...current, currency: event.target.value.toUpperCase() }))} /></label>
              {priceError ? <ErrorState error={priceError} /> : null}
              <button className="primaryButton" type="submit" disabled={createPriceCatalog.isPending}><Plus size={16} /><span>创建草稿</span></button>
            </form>
          ) : null}
        </div>
      ) : null}

      {tab === "cookies" ? (
        <div className="settingsGrid">
          <section className="surface formSection">
            <div className="sectionHeader">
              <h2>对标视频 Cookie 状态</h2>
              {cookieStatus.isLoading ? <span>加载中</span> : null}
            </div>
            {cookieStatus.error ? <ErrorState error={cookieStatus.error} /> : null}
            {cookieStatus.data ? (
              <div className="listTable">
                <div className="listRow">
                  <div>
                    <strong>{cookieStatus.data.cookie.cookie_present ? "已配置 Cookie" : "未配置 Cookie"}</strong>
                    <span>
                      {cookieStatus.data.cookie.cookie_count} 条 ·{" "}
                      {cookieStatus.data.cookie.expired ? "已过期" : "有效"}
                      {cookieStatus.data.cookie.earliest_expiry
                        ? ` · 最早 ${new Date(cookieStatus.data.cookie.earliest_expiry).toLocaleString()} 过期`
                        : ""}
                    </span>
                    <span>无头浏览器(Playwright)：{cookieStatus.data.playwright_available ? "可用" : "未安装"}</span>
                  </div>
                </div>
              </div>
            ) : null}
            <p>
              抖音必须登录后才能取到视频。两种取法都支持（格式保持「自动识别」即可）：
            </p>
            <p>
              ① <strong>应用程序面板（最简单）</strong>：F12 → 应用程序 → Cookie → https://www.douyin.com
              → 在右侧表格里点一行后按 Ctrl+A 全选 → Ctrl+C → 粘到下方。表格的「名称/值」会被自动识别。
            </p>
            <p>
              ② <strong>Network 请求头</strong>：F12 → Network → 点任一 douyin.com 请求 → Request Headers
              里复制整段 <code>cookie:</code> 值 → 粘到下方。
            </p>
            <p>Cookie 一般几天到几周会过期，过期后重新复制导入即可。</p>
          </section>

          <form
            className="surface formSection"
            onSubmit={(event) => {
              event.preventDefault();
              setCookieError(null);
              importCookies.mutate();
            }}
          >
            <h2>导入 / 测试 Cookie</h2>
            <label>
              <span>格式</span>
              <select value={cookieFormat} onChange={(event) => setCookieFormat(event.target.value as typeof cookieFormat)}>
                <option value="auto">自动识别</option>
                <option value="header">Cookie 请求头</option>
                <option value="netscape">Netscape cookies.txt</option>
                <option value="json">JSON 导出</option>
              </select>
            </label>
            <label>
              <span>Cookie 内容</span>
              <textarea
                rows={6}
                value={cookieText}
                onChange={(event) => setCookieText(event.target.value)}
                placeholder="sessionid=...; ttwid=...; sid_guard=...; ..."
                required
              />
            </label>
            {cookieError ? <ErrorState error={cookieError} /> : null}
            {importCookies.data ? (
              <div className="stateBox">
                <CheckCircle2 size={16} />
                <span>{importCookies.data.message}</span>
              </div>
            ) : null}
            <div className="formActions">
              <button className="primaryButton" type="submit" disabled={importCookies.isPending || !cookieText.trim()}>
                <KeyRound size={16} />
                <span>导入</span>
              </button>
            </div>
            <div className="governActions">
              <input
                placeholder="测试链接（可选，默认用内置示例）"
                value={cookieTestUrl}
                onChange={(event) => setCookieTestUrl(event.target.value)}
              />
              <button
                className="ghostButton"
                type="button"
                disabled={testCookies.isPending}
                onClick={() => {
                  setCookieError(null);
                  testCookies.mutate();
                }}
              >
                <FlaskConical size={15} />
                <span>测试 Cookie</span>
              </button>
            </div>
            {testCookies.data ? (
              <div className="stateBox">
                {testCookies.data.success ? <CheckCircle2 size={16} /> : <ShieldAlert size={16} />}
                <span>
                  {testCookies.data.message}
                  {testCookies.data.title ? ` — ${testCookies.data.title}` : ""}
                </span>
              </div>
            ) : null}
          </form>
        </div>
      ) : null}

      {secretOp ? (
        <Modal isOpen title={secretOp.mode === "rotate" ? "轮换密钥" : "禁用密钥"} onClose={() => setSecretOp(null)}>
          <form
            className="formGrid"
            onSubmit={(event) => {
              event.preventDefault();
              if (secretOp.mode === "rotate") rotateSecret.mutate();
              else disableSecret.mutate();
            }}
          >
            <p>{secretOp.secret.provider_id} / {secretOp.secret.name}</p>
            {secretOp.mode === "rotate" ? (
              <label><span>新明文</span><input type="password" value={secretPlaintext} onChange={(event) => setSecretPlaintext(event.target.value)} required /></label>
            ) : null}
            <label><span>原因</span><input value={secretReason} onChange={(event) => setSecretReason(event.target.value)} required /></label>
            <button className="primaryButton" type="submit" disabled={!secretReason || (secretOp.mode === "rotate" && !secretPlaintext)}>
              {secretOp.mode === "rotate" ? "提交轮换" : "确认禁用"}
            </button>
          </form>
        </Modal>
      ) : null}
    </section>
  );
}
