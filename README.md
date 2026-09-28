# 工业设备数字孪生 · 第一版：带缓冲位的虚拟装配线

纯 Python、零依赖的事件驱动仿真内核。所有时间由模拟时钟推进（无真实睡眠），
设备之间只通过命名事件和缓冲位的唤醒信号协作，后续更复杂设备按同一
事件约定即可接入。

## 运行

    python demo/run_demo.py
    python -m unittest tests.test_line -v

## 模型

    source -> B0 -> S1(快) -> B1 -> S2(慢) -> sink

- twin/clock.py：仿真时钟，只进不退。
- twin/events.py：最小时间堆 + 统一事件约定
  {time, type, target, data}；设备实现 on_event(time, type, data) 即可挂接。
- twin/material.py：物料状态机 wip -> finished/scrapped，带全程路径追溯。
- twin/buffer.py：有界缓冲位，满/空变化通知上游/下游。
- twin/station.py：工位状态机 idle/busy/blocked/down。
- twin/line.py：装配线、投料源、统计守恒、暂停快照与断点续跑。

## 关键规则

- 停下不耗料：工位仅在输入有料且下游有空位时才取料开工；
  故障时加工计时冻结，修复后用剩余工时继续，停机期间不取新料。
- 堵塞反压：下游缓冲满 -> 工位停下等待 -> 上游缓冲被占满 ->
  逐级传回投料源，源头停止投料；下游取走一件后经唤醒链逐级恢复。
- 故障：故障时刻表翻译成 station.fail 事件；可配置当前在机物料
  是否报废，报废计入统计并保留原因。
- 数量守恒：任意时刻 已投入 = 已产出 + 已报废 + 在制，
  且在制数量与“缓冲占用 + 工位在机”交叉核对，check_conservation 即断言。
- 暂停/续跑：save_state(path) 保存时钟、事件队列、物料、缓冲、
  工位的完整 JSON 快照；AssemblyLine.from_state(path) 恢复后从同一
  仿真时刻接续，逐件物料终态与连续运行完全一致（测试中有等价性校验）。

## 接入新设备

1. 向 Scheduler.register(target, handler) 登记设备标识与 on_event；
2. 用 scheduler.schedule(delay, 事件名, target, data) 投递未来事件；
3. 需要上下游联动时，通过缓冲位回调或自定义 wake 零延时事件；
4. 提供 snapshot/恢复逻辑即可支持暂停续跑。
