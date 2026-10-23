# 系外行星凌星历元审计

`/app/transit_audit.py` 从地面时序观测中审计一个凌星候选的可用历元：

```text
python3 /app/transit_audit.py --input <scenario.json> --output <report.json> --ledger <ledger.jsonl>
```

修复该程序，使其生成可复算的凌星覆盖报告。所有时间均为合成的 BJD 日数；不得联网，也不得使用第三方 Python 包。

## 输入契约

输入必须是 UTF-8 JSON 对象，顶层键恰为 `candidate`、`observations`、`policy`。

`candidate` 的键恰为 `id`、`reference_midpoint_bjd`、`period_days`、`duration_minutes`、`reference_sigma_minutes`、`drift_sigma_minutes_per_epoch`。`id` 是非空字符串；中点在 `[2400000,2600000]`；周期在 `(0,10000]` 日；持续时间在 `(0,1440]` 分钟；两个不确定度均在 `[0,1440]` 分钟。

`observations` 是长度为 1 至 200 的数组。每项键恰为 `id`、`start_bjd`、`end_bjd`、`quality`、`airmass`：ID 为非空且全数组唯一的字符串，时间在 `[2400000,2600000]` 且 `end_bjd > start_bjd`，`quality` 只能为 `CLEAR`、`THIN` 或 `CLOUD`，`airmass` 在 `[1,3]`。`policy` 的键恰为 `min_coverage_fraction`、`max_airmass`、`require_clear`、`max_ephemeris_sigma_minutes`：覆盖率在 `(0,1]`，最大气团在 `[1,3]`，`require_clear` 为布尔值，不确定度上限在 `[0,1440]`。所有数值必须是有限 JSON 数值，布尔值不能冒充数值。

读取/解析输入、结构、类型或范围错误必须拒收：非零退出，stderr 首行以 `REJECT ` 开头，既有报告和账本逐字节不变，目标目录不留下程序临时文件。

## 历元、覆盖与质量

第 `n` 个凌星的中点是：

```text
midpoint(n) = reference_midpoint_bjd + n * period_days
```

其中 `n` 为整数。该凌星的区间是中点前后各 `duration_minutes / 2880` 日。一个观测区间只有同凌星区间有**严格正**的交集时，才构成该凌星的候选观测；端点恰好相接不构成候选。报告列出所有候选历元，按 `epoch` 升序。

可计入覆盖率的候选观测还必须满足 `airmass <= max_airmass`；当 `require_clear` 为真时质量必须为 `CLEAR`，否则质量可为 `CLEAR` 或 `THIN`，`CLOUD` 永不计入。每个候选历元的覆盖率为其所有可计入观测与凌星区间交集的**并集长度**除以凌星持续时间；重叠观测不能重复累加。`qualifying_visit_ids` 为参与该历元、可计入且有严格正交集的观测 ID，按字典序排序。

该历元的预报中点不确定度（分钟）为：

```text
sqrt(reference_sigma_minutes^2 + (abs(epoch) * drift_sigma_minutes_per_epoch)^2)
```

覆盖率达到 `min_coverage_fraction`（相等时达到）且不确定度不大于 `max_ephemeris_sigma_minutes` 的历元状态为 `COVERED`。覆盖率达到阈值而不确定度超过上限的状态为 `TIMING_REVIEW`；其余为 `LOW_COVERAGE`。

顶层状态：没有候选历元时为 `NO_TRANSIT`；存在至少一个 `TIMING_REVIEW` 时为 `REVIEW`；否则存在至少一个 `COVERED` 时为 `READY`；其余为 `INSUFFICIENT`。这个顺序是固定的。

## 输出、账本与原子性

成功报告必须是 UTF-8 紧凑 JSON、对象键字典序稳定并以单个 LF 结尾。顶层字段恰为 `candidate_id`、`epochs`、`status`、`summary`。每个 `epochs` 项字段恰为 `coverage_fraction`、`epoch`、`midpoint_bjd`、`qualifying_visit_ids`、`sigma_minutes`、`status`；`summary` 字段恰为 `candidate_epochs`、`covered_epochs`、`timing_review_epochs`。`coverage_fraction` 与 `sigma_minutes` 四舍五入到 6 位小数，`midpoint_bjd` 四舍五入到 8 位小数。报告中数组、字段和数值必须稳定。

账本每行是紧凑、键排序的 JSON 事件，字段恰为 `candidate_id`、`fingerprint`、`qualified_epochs`、`status`。`fingerprint` 是原始输入对象按键排序、紧凑分隔符、UTF-8 编码后所得的小写 SHA-256。相同规范输入重复运行只能保留一条事件；不同规范输入追加一条事件。`qualified_epochs` 是 `COVERED` 历元数。

成功时必须以同目录临时文件完成报告和账本更新后再替换目标；账本读取失败或任一输出无法准备时，两个既有目标均不改写，且不留下程序临时文件。相同合法输入重复执行时，报告和账本的字节必须稳定。
