# 工业设备数字孪生仿真

本项目用纯 Python 标准库实现离散事件驱动的虚拟产线，不使用墙钟等待。

- 第一版 `AssemblyLine`：单机型串联产线、缓冲堵塞、故障与修复。
- 第二版 `ProductionLine`：多机型分流、机型切换准备时间、返工循环、抽检放行与处置、方案对比、跨状态保存恢复。

## 运行

```bash
python demo.py                # 第一版：堵塞 + 故障
python demo_production.py     # 第二版：多机型 + 准备 + 抽检返工 + 方案对比
python -m unittest discover -s tests -v
```

## 第二版模型（`digital_twin/production.py`）

- `MaterialType`：机型名、工艺路线（工位序列，可与其他机型共享/分叉）、返工上限、每工位准备时间。
- `ProductionLine`：工位、每工位进料缓冲、工序间缓冲、返工缓冲均有容量上限；满了就阻塞上游，物料不会被提前移除。
- `InspectionPolicy`：某工位后的抽检策略
  - `sampling_rate`：抽检比例；未抽中直接放行。
  - `pass_rate` + `duration`：抽中后按耗时自动出结果；`duration=0` 表示等待人工 `record_inspection()`。
  - `fail_decision`：不合格默认 `rework`（送 `rework_to` 工位）或 `scrap`。
  - `on_limit`：到达返工上限后必须明确处置——`scrap`、`concession`（让步放行）或 `hold`（等待 `dispose()` 人工决定）。
- 每件物料有唯一 `serial_number`，返工沿用同一身份；`trace()` 可查完整工位经历、返工次数与全部抽检记录，返工不计新投料。
- 抽检未出结果时，物料真实停在 `inspection:<工位>` 位置并占用该工位（`location_of()` 可查、计入在制），不会先释放空间。
- `compare_scenarios()`：在相同投料与随机种子下对比不同抽检安排的产出、报废、工位阻塞时长、准备时长与待检数量。
- `save()` / `load()`：可在准备中、待检中、返工途中保存并继续；反序列化后所有位置引用重绑到同一物料台账，重复读取不会再次投料或重复抽检已完成项目。

### 典型用法

```python
from digital_twin import ProductionLine, MaterialType, InspectionPolicy

line = ProductionLine({"S1": 3, "S2": 4, "S3": 2},
                      feed_capacity=2, buffer_capacity=1, rework_capacity=1, seed=1)
line.register_type(MaterialType("A", ["S1", "S2", "S3"], max_rework=2,
                                setup_times={"S1": 2}))
line.set_inspection(InspectionPolicy("S2", sampling_rate=1.0, pass_rate=0.8,
                                    duration=1.0, rework_to="S1",
                                    fail_decision="rework", on_limit="hold"))
line.release("A", 10)              # 缓冲不足的部分留在上游，不丢单
line.run_until(50)

for item in line.pending_inspections():
    line.record_inspection(item["serial_number"], passed=True)  # duration=0 时
for serial in (...):
    line.dispose(serial, "scrap")  # on_limit='hold' 时的明确处置

line.save("paused.json")           # 准备/待检/返工途中均可
resumed = ProductionLine.load("paused.json")
assert resumed.accounts_balanced()
```

## 账目恒等式

按总量与每个机型分别成立：

```text
已投物料 = 已产出（含让步放行） + 在制（工位/缓冲/返工/待检） + 已报废
```

`reconcile()` 返回各机型的 `released / completed / work_in_progress / scrapped / concessions`，`accounts_balanced()` 同时校验物料台账中每件物料只被计一次。
