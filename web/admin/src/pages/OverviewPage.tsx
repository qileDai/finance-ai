import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Card, Radio } from "antd";
import type { EChartsOption } from "echarts";
import { api, type DurationStats, type OverviewResponse, type StageStats } from "../api";
import { OpsChart } from "../components/OpsChart";
import { pct, StateBox } from "../components/ui";

const COLORS = {
  accent: "#0f8a6a",
  ok: "#027a48",
  danger: "#b42318",
  warn: "#b54708",
  muted: "#5b6b7c",
  ink: "#1c2430",
  admin: "#1677ff",
};

const AREA = {
  muted: "rgba(91, 107, 124, 0.10)",
  ok: "rgba(2, 122, 72, 0.12)",
  admin: "rgba(22, 119, 255, 0.10)",
  accent: "rgba(15, 138, 106, 0.12)",
};

const EMPTY_DUR: DurationStats = {
  count: 0,
  avg_seconds: 0,
  p50_seconds: 0,
  p95_seconds: 0,
  avg_minutes: 0,
  p50_minutes: 0,
  p95_minutes: 0,
};

const EMPTY_STAGE: StageStats = {
  success: 0,
  failed: 0,
  success_rate: 0,
  fail_rate: 0,
  duration: EMPTY_DUR,
};

const HOURS_OPTIONS = [
  { label: "近 24 小时", value: 24 },
  { label: "近 7 天", value: 168 },
  { label: "全部", value: 0 },
];

function emptyGraphic(show: boolean): EChartsOption["graphic"] {
  if (!show) return undefined;
  return {
    type: "text",
    left: "center",
    top: "middle",
    style: {
      text: "暂无数据",
      fill: COLORS.muted,
      fontSize: 14,
    },
  };
}

function axisStyle() {
  return {
    axisLabel: { color: COLORS.muted },
    axisLine: { lineStyle: { color: "#c9d4df" } },
    splitLine: { lineStyle: { color: "#e8eef3", type: "dashed" as const } },
  };
}

function cardTitle(title: string, caption?: string): ReactNode {
  return (
    <span className="ops-card-title">
      {title}
      {caption ? <span className="ops-card-caption">{caption}</span> : null}
    </span>
  );
}

function OpsKpi({
  label,
  value,
  sub,
  tone = "neutral",
}: {
  label: string;
  value: string | number;
  sub?: string;
  tone?: "ok" | "warn" | "neutral";
}) {
  return (
    <div className={`ops-kpi is-${tone}`}>
      <div className="ops-kpi-label">{label}</div>
      <div className="ops-kpi-value">{value}</div>
      {sub ? <div className="ops-kpi-sub">{sub}</div> : null}
    </div>
  );
}

function backlogOption(backlog: OverviewResponse["backlog"]): EChartsOption {
  const labels = ["注册排队", "注册执行中", "待审核", "账号待激活", "待填表"];
  const values = [
    backlog.register_pending,
    backlog.register_running,
    backlog.awaiting_review,
    backlog.activation_pending,
    backlog.form_pending,
  ];
  const empty = values.every((n) => n === 0);
  return {
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    grid: { left: 96, right: 28, top: 8, bottom: 24 },
    xAxis: { type: "value", minInterval: 1, ...axisStyle() },
    yAxis: {
      type: "category",
      data: labels,
      inverse: true,
      axisLabel: { color: COLORS.ink },
      axisLine: { show: false },
      axisTick: { show: false },
    },
    series: [
      {
        type: "bar",
        data: values,
        barWidth: 14,
        itemStyle: { color: COLORS.accent, borderRadius: [0, 6, 6, 0] },
        label: { show: !empty, position: "right", color: COLORS.muted },
      },
    ],
    graphic: emptyGraphic(empty),
  };
}

function stageBarOption(stages: OverviewResponse["stages"]): EChartsOption {
  const labels = ["注册", "激活", "填表"];
  const list = [stages.register, stages.activation, stages.form];
  const success = list.map((s) => s.success);
  const failed = list.map((s) => s.failed);
  const empty = success.every((n) => n === 0) && failed.every((n) => n === 0);
  return {
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      formatter: (params) => {
        const items = Array.isArray(params) ? params : [params];
        const name = String(items[0]?.name || "");
        const idx = labels.indexOf(name);
        const st = idx >= 0 ? list[idx] : EMPTY_STAGE;
        const succ = Number(items.find((i) => i.seriesName === "成功")?.value ?? st.success);
        const fail = Number(items.find((i) => i.seriesName === "失败")?.value ?? st.failed);
        return [
          name,
          `成功 ${succ}（成功率 ${pct(st.success_rate)}）`,
          `失败 ${fail}（失败率 ${pct(st.fail_rate)}）`,
        ].join("<br/>");
      },
    },
    legend: { data: ["成功", "失败"], top: 0, textStyle: { color: COLORS.muted } },
    grid: { left: 48, right: 16, top: 40, bottom: 28 },
    xAxis: { type: "category", data: labels, axisLabel: { color: COLORS.ink }, axisTick: { show: false } },
    yAxis: { type: "value", minInterval: 1, ...axisStyle() },
    series: [
      {
        name: "成功",
        type: "bar",
        stack: "stage",
        data: success,
        barMaxWidth: 36,
        itemStyle: { color: COLORS.ok, borderRadius: [0, 0, 0, 0] },
      },
      {
        name: "失败",
        type: "bar",
        stack: "stage",
        data: failed,
        barMaxWidth: 36,
        itemStyle: { color: COLORS.danger, borderRadius: [6, 6, 0, 0] },
      },
    ],
    graphic: emptyGraphic(empty),
  };
}

function durationOption(stages: OverviewResponse["stages"]): EChartsOption {
  const labels = ["注册", "激活", "填表"];
  const list = [stages.register, stages.activation, stages.form];
  const avg = list.map((s) => s.duration.avg_minutes);
  const p50 = list.map((s) => s.duration.p50_minutes);
  const p95 = list.map((s) => s.duration.p95_minutes);
  const empty = list.every((s) => s.duration.count === 0);
  const s03a = stages.register.s03a_duration?.avg_minutes ?? 0;
  return {
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      formatter: (params) => {
        const items = Array.isArray(params) ? params : [params];
        const name = String(items[0]?.name || "");
        const idx = labels.indexOf(name);
        const st = idx >= 0 ? list[idx] : EMPTY_STAGE;
        const lines = [
          `${name}（样本 ${st.duration.count}）`,
          ...items.map((i) => `${i.seriesName} ${Number(i.value).toFixed(2)} 分`),
        ];
        if (name === "注册") {
          lines.push(`S03A 平均 ${s03a.toFixed(2)} 分（不含审核等待）`);
        }
        return lines.join("<br/>");
      },
    },
    legend: { data: ["平均", "P50", "P95"], top: 0, textStyle: { color: COLORS.muted } },
    grid: { left: 48, right: 16, top: 40, bottom: 28 },
    xAxis: { type: "category", data: labels, axisLabel: { color: COLORS.ink }, axisTick: { show: false } },
    yAxis: { type: "value", name: "分钟", ...axisStyle() },
    series: [
      {
        name: "平均",
        type: "bar",
        data: avg,
        barMaxWidth: 18,
        itemStyle: { color: COLORS.accent, borderRadius: [4, 4, 0, 0] },
      },
      {
        name: "P50",
        type: "bar",
        data: p50,
        barMaxWidth: 18,
        itemStyle: { color: COLORS.admin, borderRadius: [4, 4, 0, 0] },
      },
      {
        name: "P95",
        type: "bar",
        data: p95,
        barMaxWidth: 18,
        itemStyle: { color: COLORS.warn, borderRadius: [4, 4, 0, 0] },
      },
    ],
    graphic: emptyGraphic(empty),
  };
}

function trendOption(daily: OverviewResponse["daily"]): EChartsOption {
  const dates = daily.map((d) => d.date);
  const empty = !daily.length || daily.every(
    (d) => d.created + d.succeeded + d.activated + d.form_filled === 0,
  );
  return {
    tooltip: { trigger: "axis" },
    legend: {
      data: ["创建", "注册成功", "激活成功", "填表成功"],
      top: 0,
      itemWidth: 12,
      itemGap: 10,
      textStyle: { color: COLORS.muted, fontSize: 12 },
    },
    grid: { left: 48, right: 20, top: 40, bottom: 28 },
    xAxis: { type: "category", data: dates, axisLabel: { color: COLORS.muted }, boundaryGap: false },
    yAxis: { type: "value", minInterval: 1, ...axisStyle() },
    series: [
      {
        name: "创建",
        type: "line",
        data: daily.map((d) => d.created),
        smooth: true,
        symbol: "circle",
        symbolSize: 6,
        lineStyle: { width: 2 },
        itemStyle: { color: COLORS.muted },
        areaStyle: { color: AREA.muted },
      },
      {
        name: "注册成功",
        type: "line",
        data: daily.map((d) => d.succeeded),
        smooth: true,
        symbol: "circle",
        symbolSize: 6,
        lineStyle: { width: 2 },
        itemStyle: { color: COLORS.ok },
        areaStyle: { color: AREA.ok },
      },
      {
        name: "激活成功",
        type: "line",
        data: daily.map((d) => d.activated),
        smooth: true,
        symbol: "circle",
        symbolSize: 6,
        lineStyle: { width: 2 },
        itemStyle: { color: COLORS.admin },
        areaStyle: { color: AREA.admin },
      },
      {
        name: "填表成功",
        type: "line",
        data: daily.map((d) => d.form_filled),
        smooth: true,
        symbol: "circle",
        symbolSize: 6,
        lineStyle: { width: 2 },
        itemStyle: { color: COLORS.accent },
        areaStyle: { color: AREA.accent },
      },
    ],
    graphic: emptyGraphic(empty),
  };
}

export function OverviewPage({ refreshKey }: { refreshKey: number }) {
  const [hours, setHours] = useState(24);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<OverviewResponse | null>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    api
      .overview(hours)
      .then((d) => {
        if (alive) setData(d);
      })
      .catch((e: Error) => {
        if (alive) setError(e.message || "加载失败");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [refreshKey, hours]);

  const extras = data?.extras ?? {
    created: 0,
    e2e_filled: 0,
    e2e_rate: 0,
    e2e_avg_minutes: 0,
    review_rejected: 0,
    review_approved: 0,
    review_reject_rate: 0,
    id_already_registered: 0,
    form_retries: 0,
    source: { admin: 0, wework: 0, other: 0 },
  };
  const s03aAvg = data?.stages?.register?.s03a_duration?.avg_minutes ?? 0;

  const charts = useMemo(() => {
    const backlog = data?.backlog ?? {
      register_pending: 0,
      register_running: 0,
      awaiting_review: 0,
      activation_pending: 0,
      form_pending: 0,
    };
    const stages = data?.stages ?? {
      register: EMPTY_STAGE,
      activation: EMPTY_STAGE,
      form: EMPTY_STAGE,
    };
    const daily = data?.daily ?? [];
    return {
      backlog: backlogOption(backlog),
      stages: stageBarOption(stages),
      duration: durationOption(stages),
      trend: trendOption(daily),
    };
  }, [data]);

  return (
    <StateBox loading={loading} error={error}>
      <div className="ops-stats">
        <div className="ops-stats-toolbar">
          <Radio.Group
            optionType="button"
            buttonStyle="solid"
            value={hours}
            options={HOURS_OPTIONS}
            onChange={(e) => setHours(Number(e.target.value))}
          />
          <span className="ops-stats-meta">积压为实时快照，其余为窗口内</span>
        </div>

        <div className="ops-kpi-row">
          <OpsKpi label="窗口创建" value={extras.created} />
          <OpsKpi
            label="端到端完成"
            value={extras.e2e_filled}
            sub={`完成率 ${pct(extras.e2e_rate)}`}
            tone="ok"
          />
          <OpsKpi
            label="端到端平均耗时"
            value={Number(extras.e2e_avg_minutes || 0).toFixed(1)}
            sub="分钟"
          />
          <OpsKpi
            label="审核拒绝"
            value={extras.review_rejected}
            sub={`拒绝率 ${pct(extras.review_reject_rate)}`}
            tone={extras.review_rejected > 0 ? "warn" : "neutral"}
          />
          <OpsKpi label="证件已注册" value={extras.id_already_registered} />
          <OpsKpi label="填表重试" value={extras.form_retries} />
        </div>

        <div className="ops-stats-grid">
          <Card size="small" title={cardTitle("当前积压", "实时队列快照")}>
            <OpsChart option={charts.backlog} height={280} />
          </Card>
          <Card size="small" title={cardTitle("按日趋势", "窗口内按日汇总")}>
            <OpsChart option={charts.trend} height={280} />
          </Card>
        </div>

        <div className="ops-stats-grid">
          <Card size="small" title={cardTitle("各环节成功 / 失败", "窗口内件数")}>
            <OpsChart option={charts.stages} />
          </Card>
          <Card
            size="small"
            title={cardTitle(
              "各环节耗时",
              `S03A 平均 ${Number(s03aAvg || 0).toFixed(1)} 分 · 不含审核等待`,
            )}
          >
            <OpsChart option={charts.duration} />
          </Card>
        </div>
      </div>
    </StateBox>
  );
}
