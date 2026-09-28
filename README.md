# 工业设备数字孪生仿真（第一版）

本项目用纯 Python 标准库实现一条带缓冲位的虚拟装配线。仿真通过离散事件队列推进，不使用墙钟等待。

## 模型

- `Station`：单物料工位，状态包括 `idle`、`processing`、`blocked`、`down`。
- `Buffer`：先进先出缓冲位；缓冲满时，上游工位不能继续消耗物料。
- `MaterialSource` 与 `Sink`：分别负责投放和接收物料。
- `EventQueue`：按仿真时间维护加工完成、故障、修复事件，事件会随快照保存。
- `AssemblyLine`：串联设备，传播堵塞、故障恢复和物料台账。

## 运行

```bash
python demo.py
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
