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
- `schedule_inspection_result(time, serial, station, result, visit)` / `deliver_inspection_result(serial, station, result, visit)` 必须携带检测轮次 `visit`；系统同时核对物料、工位和当轮 `serial|station|visit` 身份。
- 重复结果按幂等忽略；属于旧轮次或无法确定归属的迟到、更正结果记入 `inspection_log` 并保持待定，不会自动套用到最新一轮。
- 返工在同一物料身份上累加 `rework_count`，达到 `max_reworks` 后报废并保留原因与全部加工历史。
- `reconcile_by_type()` 按类型核对投入、在制、完成、报废；`material_history(serial)` 追溯单件轨迹。

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
