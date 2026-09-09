import { useEffect, useMemo, useState } from "react";
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
  wework: "#0f8a6a",
  other: "#8fa0b3",
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
    splitLine: { lineStyle: { color: "#e8eef3" } },
  };
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
    grid: { left: 96, right: 24, top: 16, bottom: 28 },
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
        barWidth: 16,
        itemStyle: { color: COLORS.accent, borderRadius: [0, 4, 4, 0] },
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
    xAxis: { type: "category", data: labels, axisLabel: { color: COLORS.ink } },
    yAxis: { type: "value", minInterval: 1, ...axisStyle() },
    series: [
      {
        name: "成功",
        type: "bar",
        stack: "stage",
        data: success,
        itemStyle: { color: COLORS.ok },
      },
      {
        name: "失败",
        type: "bar",
        stack: "stage",
        data: failed,
        itemStyle: { color: COLORS.danger },
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
    xAxis: { type: "category", data: labels, axisLabel: { color: COLORS.ink } },
    yAxis: { type: "value", name: "分钟", ...axisStyle() },
    series: [
      { name: "平均", type: "bar", data: avg, itemStyle: { color: COLORS.accent } },
      { name: "P50", type: "bar", data: p50, itemStyle: { color: COLORS.admin } },
      { name: "P95", type: "bar", data: p95, itemStyle: { color: COLORS.warn } },
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
      textStyle: { color: COLORS.muted },
    },
    grid: { left: 48, right: 20, top: 40, bottom: 28 },
    xAxis: { type: "category", data: dates, axisLabel: { color: COLORS.muted } },
    yAxis: { type: "value", minInterval: 1, ...axisStyle() },
    series: [
      {
        name: "创建",
        type: "line",
        data: daily.map((d) => d.created),
        smooth: true,
        itemStyle: { color: COLORS.muted },
      },
      {
        name: "注册成功",
        type: "line",
        data: daily.map((d) => d.succeeded),
        smooth: true,
        itemStyle: { color: COLORS.ok },
      },
      {
        name: "激活成功",
        type: "line",
        data: daily.map((d) => d.activated),
        smooth: true,
        itemStyle: { color: COLORS.admin },
      },
      {
        name: "填表成功",
        type: "line",
        data: daily.map((d) => d.form_filled),
        smooth: true,
        itemStyle: { color: COLORS.accent },
      },
    ],
    graphic: emptyGraphic(empty),
  };
}

function pieOption(
  items: { name: string; value: number; color: string }[],
): EChartsOption {
  const empty = items.every((i) => i.value === 0);
  return {
    tooltip: { trigger: "item", formatter: "{b}: {c}（{d}%）" },
    legend: { bottom: 0, textStyle: { color: COLORS.muted } },
    series: [
      {
        type: "pie",
        radius: ["42%", "68%"],
        center: ["50%", "44%"],
        data: items.map((i) => ({
          name: i.name,
          value: i.value,
          itemStyle: { color: i.color },
        })),
        label: { color: COLORS.ink, formatter: empty ? "" : "{b}\n{d}%" },
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
    const extra = data?.extras ?? {
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
    const daily = data?.daily ?? [];
    return {
      backlog: backlogOption(backlog),
      stages: stageBarOption(stages),
      duration: durationOption(stages),
      trend: trendOption(daily),
      source: pieOption([
        { name: "后台快速注册", value: extra.source?.admin ?? 0, color: COLORS.admin },
        { name: "企微群", value: extra.source?.wework ?? 0, color: COLORS.wework },
        { name: "其他", value: extra.source?.other ?? 0, color: COLORS.other },
      ]),
      e2e: pieOption([
        { name: "端到端完成", value: extra.e2e_filled, color: COLORS.ok },
        {
          name: "未完成",
          value: Math.max(0, extra.created - extra.e2e_filled),
          color: COLORS.other,
        },
      ]),
    };
  }, [data]);

  return (
    <StateBox loading={loading} error={error}>
      <div className="ops-stats-toolbar">
        <Radio.Group
          optionType="button"
          buttonStyle="solid"
          value={hours}
          options={HOURS_OPTIONS}
          onChange={(e) => setHours(Number(e.target.value))}
        />
        <span className="ops-stats-meta">
          窗口内创建 {extras.created} · 端到端完成 {extras.e2e_filled}（
          {pct(extras.e2e_rate)}）· 审核拒绝 {extras.review_rejected}（
          {pct(extras.review_reject_rate)}）· 证件已注册 {extras.id_already_registered}{" "}
          · 填表重试 {extras.form_retries} · 端到端平均 {Number(extras.e2e_avg_minutes || 0).toFixed(1)} 分
        </span>
      </div>

      <div className="ops-stats-grid">
        <Card size="small" title="当前积压" extra="实时队列">
          <OpsChart option={charts.backlog} />
        </Card>
        <Card size="small" title="各环节成功 / 失败">
          <OpsChart option={charts.stages} />
        </Card>
        <Card
          size="small"
          title="各环节耗时"
          extra={`S03A 平均 ${Number(s03aAvg || 0).toFixed(1)} 分`}
        >
          <OpsChart option={charts.duration} />
        </Card>
        <Card size="small" title="按日趋势">
          <OpsChart option={charts.trend} />
        </Card>
      </div>

      <div className="ops-stats-pies">
        <Card size="small" title="来源分布">
          <OpsChart option={charts.source} height={280} />
        </Card>
        <Card size="small" title="端到端完成" extra={`完成率 ${pct(extras.e2e_rate)}`}>
          <OpsChart option={charts.e2e} height={280} />
        </Card>
      </div>
    </StateBox>
  );
}
