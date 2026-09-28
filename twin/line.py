"""虚拟装配线：把源点、缓冲位、工位和事件调度器接成一条线。

物料守恒恒等式（任意时刻成立）::

    已投入 = 已产出 + 已报废 + 在制(WIP)

在制 = 线首缓冲 + 各工位在机 + 工位间缓冲。
暂停时把时钟、事件队列、物料、缓冲、工位整体快照落盘，
恢复后从同一仿真时刻接续，守恒恒等式不受影响。
"""

import json
from typing import Any, Callable, Dict, List, Optional

from .buffer import Buffer
from .clock import Clock
from .events import Scheduler
from .material import Material, WIP, FINISHED, SCRAPPED
from .station import Station, FailureModel, EV_FAIL, EV_WAKE

EV_SOURCE_TICK = "source.tick"
SOURCE_ID = "source"
SINK_ID = "sink"


class AssemblyLine:
    """顺序装配线：source -> buf0 -> st1 -> buf1 -> st2 -> ... -> sink。"""

    def __init__(
        self,
        name: str,
        station_specs: List[Dict[str, Any]],
        source_interval: float,
        buffer_capacities: Optional[List[int]] = None,
        total_materials: Optional[int] = None,
        log: Optional[Callable[[str], None]] = print,
    ):
        # station_specs: [{"id","process_time","failures":[(t,dur,scrap,reason), ...]}]
        self.name = name
        self.source_interval = float(source_interval)
        self.total_materials = total_materials
        self._log_fn = log
        self.config = {
            "name": name,
            "source_interval": self.source_interval,
            "total_materials": total_materials,
            "station_specs": station_specs,
            "buffer_capacities": buffer_capacities,
        }

        self.clock = Clock(0.0)
        self.scheduler = Scheduler(0.0)
        self.materials: Dict[str, Material] = {}
        self._seq = 0
        self.produced = 0
        self.scrapped = 0
        self.loaded = 0
        self.trace: List[str] = []

        n = len(station_specs)
        caps = buffer_capacities or [1] * n
        if len(caps) != n:
            raise ValueError("缓冲容量个数应与工位数一致（含线首进料缓冲）")
        self.buffers: List[Buffer] = []
        for i, cap in enumerate(caps):
            self.buffers.append(Buffer(f"B{i}", int(cap)))

        self.stations: List[Station] = []
        for i, spec in enumerate(station_specs):
            in_buf = self.buffers[i]
            out_buf = None if i == n - 1 else self.buffers[i + 1]
            failures = FailureModel(spec.get("failures"))
            station = Station(spec["id"], float(spec["process_time"]), in_buf, out_buf, failures)
            self.stations.append(station)

        for station in self.stations:
            station.bind(
                self.scheduler,
                self.scheduler,
                self._on_finish_material,
                self._on_scrap_material,
                self._log,
            )
        # 源点作为一个虚拟设备登记到同一事件约定
        self.scheduler.register(SOURCE_ID, self._on_source_event)
        # 首个进料缓冲腾出空位时，唤醒源点补料
        self.buffers[0].on_space_available = self._wake_source

    # ---- 日志 ----
    def _log(self, msg: str) -> None:
        line = msg
        self.trace.append(line)
        if self._log_fn:
            self._log_fn(line)

    # ---- 源点 ----
    def _wake_source(self) -> None:
        self.scheduler.schedule(0.0, EV_WAKE, SOURCE_ID)

    def _on_source_event(self, time: float, event_type: str, data: Dict[str, Any]) -> None:
        if event_type not in (EV_SOURCE_TICK, EV_WAKE):
            return
        first = self.buffers[0]
        if not first.can_put():
            return  # 线首满：源点停下等待，堵塞反压到源头
        if self.total_materials is not None and self.loaded >= self.total_materials:
            return
        self._seq += 1
        mid = f"M{self._seq:03d}"
        material = Material(mid, time)
        self.materials[mid] = material
        self.loaded += 1
        first.put(material)  # 唤醒 S1
        self._log(f"t={time:7.2f} [source] 投入 {mid}")
        if self.total_materials is None or self.loaded < self.total_materials:
            self.scheduler.schedule(self.source_interval, EV_SOURCE_TICK, SOURCE_ID)

    # ---- 成品 / 报废 ----
    def _on_finish_material(self, material: Material) -> None:
        material.mark_finished(self.scheduler.now)
        material.move(SINK_ID)
        self.produced += 1
        self._log(f"t={self.scheduler.now:7.2f} [sink] 产出 {material.material_id}，累计 {self.produced}")

    def _on_scrap_material(self, material: Material, reason: str) -> None:
        material.mark_scrapped(self.scheduler.now, reason)
        material.move("scrap")
        self.scrapped += 1

    # ---- 运行 ----
    def start(self) -> None:
        self.scheduler.schedule(0.0, EV_SOURCE_TICK, SOURCE_ID)
        self._arm_failures(after=0.0)

    def _arm_failures(self, after: float) -> None:
        """把故障时刻表翻译成统一的 station.fail 事件；恢复时只排未发生的。"""
        for station, spec in zip(self.stations, self.config["station_specs"]):
            for start, duration, scrap, reason in spec.get("failures", []):
                if float(start) >= after - 1e-12:
                    self.scheduler.schedule_at(
                        float(start),
                        EV_FAIL,
                        station.station_id,
                        {"duration": float(duration), "scrap": bool(scrap), "reason": str(reason)},
                    )

    def run_until(self, end_time: float) -> None:
        self.scheduler.run_until(end_time)
        self.clock.advance_to(self.scheduler.now)

    # ---- 统计与守恒 ----
    def wip_count(self) -> int:
        return sum(1 for m in self.materials.values() if m.status == WIP)

    def wip_locations(self) -> Dict[str, int]:
        loc: Dict[str, int] = {}
        for m in self.materials.values():
            if m.status == WIP:
                loc[m.location] = loc.get(m.location, 0) + 1
        return dict(sorted(loc.items()))

    def conservation(self) -> Dict[str, Any]:
        wip = self.wip_count()
        return {
            "time": self.scheduler.now,
            "loaded": self.loaded,
            "produced": self.produced,
            "scrapped": self.scrapped,
            "wip": wip,
            "balanced": self.loaded == self.produced + self.scrapped + wip,
            "wip_locations": self.wip_locations(),
        }

    def check_conservation(self) -> None:
        report = self.conservation()
        if not report["balanced"]:
            raise RuntimeError(f"物料守恒被破坏: {report}")
        # 交叉校验：WIP 必须在缓冲或工位在机
        in_buffers = sum(b.level for b in self.buffers)
        on_machines = sum(1 for s in self.stations if s.material is not None)
        if in_buffers + on_machines != report["wip"]:
            raise RuntimeError(
                f"WIP 位置对不上: 缓冲 {in_buffers} + 在机 {on_machines} != {report['wip']}"
            )

    # ---- 暂停快照 / 恢复 ----
    def save_state(self, path: str) -> None:
        self.clock.advance_to(self.scheduler.now)
        self.check_conservation()
        snapshot = {
            "config": self.config,
            "clock": self.clock.snapshot(),
            "scheduler": self.scheduler.snapshot(),
            "seq": self._seq,
            "produced": self.produced,
            "scrapped": self.scrapped,
            "loaded": self.loaded,
            "materials": {k: v.snapshot() for k, v in self.materials.items()},
            "buffers": [b.snapshot() for b in self.buffers],
            "stations": [s.snapshot() for s in self.stations],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        self._log(f"t={self.scheduler.now:7.2f} [line] 暂停状态已保存 -> {path}")

    @classmethod
    def from_state(
        cls,
        path: str,
        log: Optional[Callable[[str], None]] = print,
        replay_trace: bool = False,
    ) -> "AssemblyLine":
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        cfg = state["config"]
        line = cls(
            cfg["name"],
            cfg["station_specs"],
            cfg["source_interval"],
            cfg.get("buffer_capacities"),
            cfg.get("total_materials"),
            log=log if replay_trace else (lambda _msg: None),
        )
        line.clock = Clock.restore(state["clock"])

        line._seq = int(state["seq"])
        line.produced = int(state["produced"])
        line.scrapped = int(state["scrapped"])
        line.loaded = int(state["loaded"])
        line.materials = {
            k: Material.restore(v) for k, v in state["materials"].items()
        }

        # 恢复缓冲占位
        for buf_state, buf in zip(state["buffers"], line.buffers):
            buf._slots = [line.materials[mid] for mid in buf_state["slots"]]

        # 恢复工位状态与在机物料
        for st_state, station in zip(state["stations"], line.stations):
            station.failures = FailureModel.restore(st_state["failures"])
            station.attach_restore_state(st_state)
        for station in line.stations:
            station.restore_material_ref(line.materials)

        # 先恢复事件队列，再把设备重新接到该调度器（清掉构造时的旧监听）
        handlers = {SOURCE_ID: line._on_source_event}
        for station in line.stations:
            handlers[station.station_id] = station.on_event
        line.scheduler = Scheduler.restore(state["scheduler"], handlers)
        for buf in line.buffers:
            buf._space_listeners = []
            buf._material_listeners = []
        for station in line.stations:
            station.bind(
                line.scheduler,
                line.scheduler,
                line._on_finish_material,
                line._on_scrap_material,
                log,
            )
        # 恢复后各缓冲的回调接线保持构造时设置（B0 的 source 唤醒也在）
        line.buffers[0].on_space_available = line._wake_source
        # 只唤醒“停下等待环境变化”的设备；BUSY/DOWN 由其已保存的未来事件驱动
        from .station import IDLE, BLOCKED
        for station in line.stations:
            if station.state in (IDLE, BLOCKED):
                station.wake()
        line._wake_source()
        line.check_conservation()
        if not replay_trace and log:
            log(f"t={line.scheduler.now:7.2f} [line] 已从 {path} 接续，事件队列 {len(state['scheduler']['heap'])} 条")
        return line
