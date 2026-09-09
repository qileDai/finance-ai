---
name: nnc1-prelim-check
description: >-
  Classifies NNC1 初步检查 as pass (通过/通過) or reject (拒绝/拒絕), scrapes the
  result cell, and sends the WeCom review-group markdown (green info / orange
  warning). Use when the user mentions 初步检查, 初步檢查結果, 通过, 通過,
  拒绝, 拒絕, 群通知, prelim check, NNC1 step 6, or edits icris_nnc1_form.py /
  icris_form_notify.py.
---

# NNC1 初步检查判定与群通知

填表停在步骤 6 初步检查页：**不要点「继续」，不要进付款页**。出结果后立刻 `persist_form_outcome` + `notify_prelim_result`。

## 判定

只认结果格（简繁都要）：

| 结果格 | `prelim_check_passed` |
|--------|------------------------|
| `通过` / `通過`（且不是不通过/拒絕） | `True` |
| `拒絕` / `拒绝` / `不通过` / `Rejection` | `False` |
| 空，但刮到拒絕原因 | `False` |

`通過。請按「繼續」按鈕以完成提交過程。` 是通过。旁路刮到的「拒絕原因」**不得**把明确通过改判失败。

同页多段「初步檢查結果」仍用 `pick_best_prelim_result`（拒絕优先），避免拒绝页上的「请按继续」抢先。

采集：相邻单元格 + ant-descriptions + 同格 `标签 ：值`。入口：

- `src/browser/icris_nnc1_form.py` — `prelim_check_passed`、`_PRELIM_*_COLLECT_JS`、`_wait_preliminary_check_and_continue`
- `.cursor/rules/nnc1-prelim-check.mdc`

## 群通知

`src/wework/icris_form_notify.py`：

- Webhook markdown：通过 `<font color="info">`，拒绝 `<font color="warning">`
- 通过消息不含拒絕原因；拒绝才附原因全文
- `send_group_text` 回退用 `strip_prelim_markdown`

细节见 `.cursor/rules/wework-prelim-notify.mdc`。

## 改完必跑

```
python -m unittest tests.test_nnc1_step3_names tests.test_icris_form_notify -v
```

通过用例须覆盖：`通过。请按继续…`、`通過。請按「繼續」…`（即使带旁路原因也算通过）；`拒絕`/`拒绝` 仍失败；同页「拒絕 + 通过请按继续」仍取拒絕。
