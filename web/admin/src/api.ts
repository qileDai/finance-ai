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
  updated_at?: string;
  finished_at?: string;
};

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
};
