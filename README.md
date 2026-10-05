# 工业设备数字孪生仿真（第一版）

本项目用纯 Python 标准库实现一条带缓冲位的虚拟装配线。仿真通过离散事件队列推进，不使用墙钟等待。

## 模型

- `Station`：单物料工位，状态包括 `idle`、`processing`、`blocked`、`down`。
- `Buffer`：先进先出缓冲位；缓冲满时，上游工位不能继续消耗物料。
- `MaterialSource` 与 `Sink`：分别负责投放和接收物料。
- `EventQueue`：按仿真时间维护加工完成、故障、修复事件，事件会随快照保存。
- `AssemblyLine`：串联设备，传播堵塞、故障恢复和物料台账。
- `MaterialType`：一类物料的工艺定义，包含路线（途经工位）、各工位节拍、换型准备时间、抽检工位与返工上限。

## 多类型物料

- 通过构造参数 `material_types` 与 `release_plan` 按类型投放，仍走原有 source → 工位 → 缓冲 → sink 路径与同一本台账。
- 换型时工位先执行准备（setup），不同节拍按类型生效；不在路线内的工位零时间穿过。
- 抽检工位加工完成后物料进入 `pending_inspection` 并真实占用工位，结果送达前后续物料无法穿过。
- `schedule_inspection_result` / `deliver_inspection_result` 通过 `visit` 携带明确的检测轮次，送达 pass / rework / fail；核对物料、工位与当轮检测身份（`序列号|工位|轮次`），不能仅凭该物料当前存在待检记录就采用。
- 当前待检轮次的重复结果按已定论轮次幂等忽略；已定论旧轮次的迟到、更正结果也幂等忽略，均记入 `inspection_log`，不会放行新一轮等待中的物料。
- 无轮次、轮次与当前待检不符、或与工位实际占用物料对不上的结果保留待定，记入 `indeterminate_results`，不会自动套用到最新一轮；待判定结果随快照保存。
- 返工在同一物料身份上累加 `rework_count`，达到 `max_reworks` 后报废并保留原因与全部加工历史。
- `reconcile_by_type()` 按类型核对投入、在制、完成、报废；`material_history(serial)` 追溯单件轨迹。

## 检测资源受限时的返工调度

- 构造参数 `inspection_capacity` 启用受限检测工位（`InspectionResource`），多个物料的不同访问轮次在同一资源上排队；`inspection_duration` 为检测估计时长，`inspection_policy` 可选 `priority`（默认）或 `fifo`。
- `MaterialType` 新增 `priority`（交付优先级）与 `due_time`（交期）；`inspection_outages` 或 `add_inspection_outage(start, end)` 指定工位停用区间，停用期间不开始新检测，装不下的作业会让位给能装下的作业，停用结束自动唤醒。
- `inspection_order()` 给出当前可执行顺序；`inspection_metrics()` 汇总总等待时间与逾期数量，便于比较不同调度策略；`InspectionResource.conserved()` 核对作业守恒（已提交 = 排队 + 在检 + 完成 + 取消）。
- 检测必须先由资源实际开始：`predict_inspection_result` 只记录预测，不构成放行；未开始的轮次收到结果会保留待定；旧轮次反馈不会提前释放当前检测占用。
- 实际失败（fail / 超返工上限 / 工位故障报废）只重排受影响的剩余路线：该物料尚未开始的检测承诺由 `cancel_for_serial` 释放，已开始的标记 `cancel_pending`，等待明确结束后才释放资源；已完成加工与已发生报废保持事实。
- `cancel_inspection(serial, station, visit)` 手动取消：未开始返回 `released` 并释放承诺，已开始返回 `deferred`。
- 检测资源状态（队列、停用区间、全部作业）随快照保存与恢复。

## 运行

```bash
python demo.py
python demo_multi_type.py
python -m unittest discover -s tests -v
```

演示场景中，`station-1` 周期为 3，`station-2` 周期为 5，中间有 1 个缓冲位。慢工位使快工位在缓冲满后停等；`station-2` 在仿真时间 17 故障，当前物料报废，6 个时间单位后修复并继续。

暂停接续使用：

```python
line.pause_at(17)
line.save("paused_line.json")

resumed = AssemblyLine.load("paused_line.json")
resumed.run_until(35)
assert resumed.accounts_balanced()
```

账目恒等式：

```text
已投物料 = 已产出 + 在制 + 已报废
```
