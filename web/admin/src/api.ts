export type ApiOk<T extends Record<string, unknown> = Record<string, unknown>> = {
  ok: true;
} & T;

export type ApiErr = { ok: false; error: string };

export type OverviewResponse = ApiOk<{
  hours: number;
  conversation: Record<string, unknown>;
  registration: Record<string, unknown>;
  icris_worker: Record<string, unknown>;
}>;

export type SessionsResponse = ApiOk<{
  items: SessionSummary[];
  channel: string;
}>;

export type SessionSummary = {
  roomid: string;
  name?: string;
  status?: string;
  company_name?: string;
  open_kfid?: string;
  material_count?: number;
  channel?: string;
  label?: string;
};

export type SessionDetailResponse = ApiOk<{
  session: SessionSummary & Record<string, unknown>;
  materials: MaterialRow[];
}>;

export type MaterialRow = {
  field_key?: string;
  status?: string;
  field_value?: string;
  value_text?: string;
  file_path?: string;
  updated_at?: string;
};

export type JobsResponse = ApiOk<{
  items: JobRow[];
  status: string;
  limit: number;
}>;

export type JobRow = {
  id: number;
  roomid: string;
  status: string;
  attempts?: number;
  max_attempts?: number;
  dry_run?: number | boolean;
  allow_submit?: number | boolean;
  last_error?: string;
  screenshot_path?: string;
  company_name?: string;
  source?: string;
  updated_at?: string;
  finished_at?: string;
  created_at?: string;
  started_at?: string;
};

export type JobLogLine = {
  level: string;
  message: string;
  time?: string;
};

export type JobField = { key: string; label?: string; value: string };

export type JobDetailResponse = ApiOk<{
  job: JobRow & {
    payload_json?: string;
    result_messages?: string;
    package_dir?: string;
    customer_id?: string;
  };
  payload: Record<string, unknown>;
  fields: JobField[];
  messages: Array<JobLogLine | string>;
}>;

export type QualityResponse = ApiOk<{
  hours: number;
  stats: Record<string, unknown>;
  low_confidence_runs: LowRun[];
}>;

export type LowRun = {
  id?: string;
  roomid?: string;
  question?: string;
  action?: string;
  confidence?: number;
  answer_score?: number;
  retrieval_score?: number;
  duration_ms?: number;
  created_at?: string;
};

export type RunnerFile = { name: string; data_url: string };

export type WeworkSendModes = {
  configured: boolean;
  kf_configured: boolean;
  send_mode: "kf" | "mass" | "webhook" | "appchat" | string;
  channel: string;
  webhook_url_set: boolean;
  default_owner_set: boolean;
};

export type WeworkSendResponse = ApiOk<{
  plan: string;
  result: Record<string, unknown>;
}>;

export type RunnerStatus = {
  status: "idle" | "running" | "succeeded" | "failed" | "pending";
  started_at?: string;
  finished_at?: string;
  messages?: Array<JobLogLine | string>;
  error?: string;
  company_name?: string;
  case_id?: string;
  job_id?: number | null;
  dry_run?: boolean;
};

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

function redirectToLogin() {
  const base = "/admin/login";
  if (!window.location.pathname.endsWith("/login")) {
    window.location.assign(base);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    credentials: "include",
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...(init?.headers || {}),
    },
  });
  const data = (await res.json().catch(() => ({
    ok: false,
    error: `HTTP ${res.status}`,
  }))) as ApiErr | T;

  if (res.status === 401 && !path.includes("/admin/api/login") && !path.includes("/admin/api/me")) {
    redirectToLogin();
    throw new ApiError((data as ApiErr).error || "unauthorized", 401);
  }

  if (!res.ok || (data as ApiErr).ok === false) {
    const err = (data as ApiErr).error || `HTTP ${res.status}`;
    throw new ApiError(err, res.status);
  }
  return data as T;
}

export const api = {
  me: () =>
    request<ApiOk<{ authenticated: boolean; username: string }>>("/admin/api/me"),
  login: (username: string, password: string) =>
    request<ApiOk<{ username: string }>>("/admin/api/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  logout: () =>
    request<ApiOk>("/admin/api/logout", { method: "POST" }),
  overview: () => request<OverviewResponse>("/admin/api/overview"),
  sessions: (channel = "all") =>
    request<SessionsResponse>(`/admin/api/sessions?channel=${encodeURIComponent(channel)}`),
  session: (roomid: string) =>
    request<SessionDetailResponse>(`/admin/api/sessions/${encodeURIComponent(roomid)}`),
  jobs: (status = "", limit = 50) => {
    const q = new URLSearchParams();
    if (status) q.set("status", status);
    q.set("limit", String(limit));
    return request<JobsResponse>(`/admin/api/jobs?${q}`);
  },
  job: (id: number) =>
    request<JobDetailResponse>(`/admin/api/jobs/${id}`),
  cancelJob: (id: number) =>
    request<ApiOk<{ job: JobRow; message: string }>>(
      `/admin/api/jobs/${id}/cancel`,
      { method: "POST" },
    ),
  requeueJob: (id: number) =>
    request<ApiOk<{ job: JobRow; message: string }>>(
      `/admin/api/jobs/${id}/requeue`,
      { method: "POST" },
    ),
  quality: (hours = 24) =>
    request<QualityResponse>(`/admin/api/quality?hours=${hours}`),
  registerRunner: {
    defaults: () =>
      request<ApiOk<{ contact_email?: string; contact_phone?: string }>>(
        "/admin/api/register-runner/defaults"
      ),
    submit: (
      fields: Record<string, string>,
      files: Record<string, RunnerFile>,
      dry_run = true
    ) =>
      request<
        ApiOk<{
          case_id: string;
          company_name: string;
          job_id?: number;
          dry_run?: boolean;
        }>
      >("/admin/api/register-runner/submit", {
        method: "POST",
        body: JSON.stringify({ fields, files, dry_run }),
      }),
    extractId: (payload: {
      data_url: string;
      filename?: string;
      current_fields?: Record<string, string>;
      fill_empty_only?: boolean;
      expected_id_type?: string;
    }) =>
      request<
        ApiOk<{
          fields: Record<string, string>;
          merged_fields?: Record<string, string>;
          need_taiwan_id?: boolean;
          hints?: string[];
          vision?: {
            id_type?: string;
            type_label?: string;
            confidence?: number;
            issuing_country?: string;
          };
        }>
      >("/admin/api/register-runner/extract-id", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    status: () =>
      request<ApiOk<RunnerStatus>>("/admin/api/register-runner/status"),
  },
  idExtract: (payload: {
    expected_id_type?: string;
    data_url: string;
    filename?: string;
  }) =>
    request<
      ApiOk<{
        fields: Record<string, string>;
        display?: { key: string; label: string; value: string }[];
        need_taiwan_id?: boolean;
        type_mismatch?: boolean;
        hints?: string[];
        vision?: {
          id_type?: string;
          type_label?: string;
          confidence?: number;
          issuing_country?: string;
        };
      }>
    >("/admin/api/id-extract", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  idTranslate: (payload: { text: string; engine: "google" | "youdao" | "deepl" }) =>
    request<ApiOk<{ translated: string; engine: string }>>(
      "/admin/api/id-extract/translate",
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    ),
  wework: {
    sendModes: () =>
      request<ApiOk<WeworkSendModes>>("/admin/api/wework/send-modes"),
    send: (chat_id: string, content: string, to_external_userid?: string) =>
      request<WeworkSendResponse>("/admin/api/wework/send", {
        method: "POST",
        body: JSON.stringify({ chat_id, content, to_external_userid }),
      }),
  },
};
