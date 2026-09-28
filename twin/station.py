"""工位设备：统一事件约定的参考实现。

工位状态机：

    IDLE 空闲可接料 --开始加工--> BUSY 加工中
    BUSY --完成且下游可放--> 推送/置缓冲，回到 IDLE
    BUSY --完成但下游满--> BLOCKED 堵塞（物料留在本机）
    IDLE/BUSY --故障--> DOWN（BUSY 时计时冻结，修好后继续剩余工时）
    BLOCKED --下游腾出空位--> 推送后 IDLE
    DOWN --修复--> 回到故障前状态并重新评估

工位停下（堵塞/故障/无料）时绝不消耗下一件物料。
"""

from typing import Any, Callable, Dict, List, Optional

from .buffer import Buffer
from .material import Material

IDLE = "idle"
BUSY = "busy"
BLOCKED = "blocked"
DOWN = "down"

EV_START = "station.start"
EV_FINISH = "station.finish"
EV_FAIL = "station.fail"
EV_REPAIR = "station.repair"
EV_WAKE = "station.wake"


class FailureModel:
    """故障计划：确定性时刻表用于演示/测试，也可由外部随机模型生成。

    每条计划: (故障开始时刻, 停机时长, 是否报废当前在机物料, 原因)。
    """

    def __init__(self, schedule: Optional[List[tuple]] = None):
        self.schedule = sorted(
            [(float(t), float(d), bool(s), str(r)) for t, d, s, r in (schedule or [])],
            key=lambda x: x[0],
        )
        self.index = 0

    def due_failures(self, upto_time: float) -> List[tuple]:
        due = []
        while self.index < len(self.schedule) and self.schedule[self.index][0] <= upto_time:
            due.append(self.schedule[self.index])
            self.index += 1
        return due

    def snapshot(self) -> Dict[str, Any]:
        return {"schedule": self.schedule, "index": self.index}

    @classmethod
    def restore(cls, state: Dict[str, Any]) -> "FailureModel":
        model = cls()
        model.schedule = [tuple(x) for x in state["schedule"]]
        model.index = int(state["index"])
        return model


class Station:
    def __init__(
        self,
        station_id: str,
        process_time: float,
        input_buffer: Optional[Buffer],
        output_buffer: Optional[Buffer],
        failure_model: Optional[FailureModel] = None,
    ):
        if process_time <= 0:
            raise ValueError("加工工时必须为正")
        self.station_id = station_id
        self.process_time = float(process_time)
        self.input_buffer = input_buffer
        self.output_buffer = output_buffer
        self.failures = failure_model or FailureModel()

        self.state = IDLE
        self.material: Optional[Material] = None
        self.remaining_time = 0.0
        self.last_resume_time: Optional[float] = None
        self.pre_down_state = IDLE

        self._scheduler = None
        self._clock = None
        self._on_finish_material: Optional[Callable[[Material], None]] = None
        self._on_scrap: Optional[Callable[[Material, str], None]] = None
        self._log: Optional[Callable[[str], None]] = None

    def bind(
        self,
        scheduler,
        clock,
        on_finish_material: Callable[[Material], None],
        on_scrap: Callable[[Material, str], None],
        log: Callable[[str], None],
    ) -> None:
        self._scheduler = scheduler
        self._clock = clock
        self._on_finish_material = on_finish_material
        self._on_scrap = on_scrap
        self._log = log
        scheduler.register(self.station_id, self.on_event, replace=True)
        if self.input_buffer is not None:
            # 输入缓冲有新料：唤醒可能在等料的本工位
            self.input_buffer.on_material_available = self.wake
        if self.output_buffer is not None:
            # 下游缓冲腾出空位：唤醒被堵塞的本工位，堵塞由此逐级向上游解除
            self.output_buffer.on_space_available = self.wake

    def _say(self, msg: str) -> None:
        if self._log:
            self._log(f"t={self._clock.now:7.2f} [{self.station_id}] {msg}")

    def wake(self) -> None:
        """环境变化信号：统一压成零延时事件，避免设备间直接调用。"""
        if self._scheduler is not None:
            self._scheduler.schedule(0.0, EV_WAKE, self.station_id)

    def on_event(self, time: float, event_type: str, data: Dict[str, Any]) -> None:
        if event_type == EV_WAKE:
            self._evaluate()
        elif event_type == EV_FINISH:
            self._on_process_finish()
        elif event_type == EV_REPAIR:
            self._on_repair()
        elif event_type == EV_FAIL:
            if self.state != DOWN:
                self._enter_failure(
                    float(data.get("duration", 0.0)),
                    bool(data.get("scrap", False)),
                    str(data.get("reason", "设备故障")),
                )

    def _evaluate(self) -> None:
        if self.state == BLOCKED:
            self._try_push()
            if self.state == BLOCKED:
                return
        if self.state != IDLE:
            return
        if self.input_buffer is None or self.input_buffer.is_empty():
            return
        if self.output_buffer is not None and not self.output_buffer.can_put():
            # 下游没有空位就不开新件：物料留在上游缓冲，堵塞反压回源头
            return
        material = self.input_buffer.take()
        self.material = material
        material.move(self.station_id)
        self.state = BUSY
        self.remaining_time = self.process_time
        self.last_resume_time = self._clock.now
        self._say(f"开始加工 {material.material_id}（工时 {self.process_time:g}）")
        self._scheduler.schedule(self.process_time, EV_FINISH, self.station_id)

    def _on_process_finish(self) -> None:
        if self.state != BUSY:
            return
        self.remaining_time = 0.0
        self.last_resume_time = None
        if not self._try_push():
            self.state = BLOCKED
            self._say(f"加工完成但下游堵塞，{self.material.material_id} 滞留本机")

    def _try_push(self) -> bool:
        """把在机物料推到下游缓冲或成品口；成功返回 True。"""
        if self.material is None:
            return True
        if self.output_buffer is not None and self.output_buffer.is_full():
            return False
        material = self.material
        if self.output_buffer is None:
            self._on_finish_material(material)
        else:
            self.output_buffer.put(material)
        self._say(f"放行 {material.material_id}")
        self.material = None
        self.state = IDLE
        # 是否接下一件由输入缓冲的“有料”通知驱动；
        # 若输入缓冲本就有料（非空变非空不发信号），主动补一次评估
        if self.input_buffer is not None and not self.input_buffer.is_empty():
            self.wake()
        return True

    def _enter_failure(self, duration: float, scrap_current: bool, reason: str) -> None:
        self.pre_down_state = self.state
        self.state = DOWN
        if self.material is not None and self.last_resume_time is not None:
            elapsed = self._clock.now - self.last_resume_time
            self.remaining_time = max(0.0, self.remaining_time - elapsed)
            self.last_resume_time = None
        if scrap_current and self.material is not None:
            material = self.material
            self._on_scrap(material, reason)
            self._say(f"故障导致 {material.material_id} 报废：{reason}")
            self.material = None
            self.pre_down_state = IDLE
            self.remaining_time = 0.0
        self._say(f"故障停机：{reason}，预计 {duration:g} 修复")
        if duration > 0:
            self._scheduler.schedule(duration, EV_REPAIR, self.station_id)
        else:
            self.wake()

    def _on_repair(self) -> None:
        if self.state != DOWN:
            return
        self._say("修复完成，恢复运行")
        if self.pre_down_state == BUSY and self.material is not None:
            self.state = BUSY
            self.last_resume_time = self._clock.now
            self._say(f"继续加工 {self.material.material_id}，剩余 {self.remaining_time:g}")
            self._scheduler.schedule(self.remaining_time, EV_FINISH, self.station_id)
        elif self.pre_down_state == BLOCKED and self.material is not None:
            self.state = BLOCKED
            if self._try_push():
                self._evaluate()
        else:
            self.state = IDLE
            self._evaluate()

    def snapshot(self) -> Dict[str, Any]:
        return {
            "station_id": self.station_id,
            "process_time": self.process_time,
            "state": self.state,
            "material_id": self.material.material_id if self.material else None,
            "remaining_time": self.remaining_time,
            "pre_down_state": self.pre_down_state,
            "failures": self.failures.snapshot(),
        }

    def attach_restore_state(self, state: Dict[str, Any]) -> None:
        self._restore_state = state

    def restore_material_ref(self, materials: Dict[str, Material]) -> None:
        state = getattr(self, "_restore_state", None)
        if state is None:
            return
        mid = state["material_id"]
        self.material = materials[mid] if mid else None
        self.state = state["state"]
        self.remaining_time = state["remaining_time"]
        self.pre_down_state = state["pre_down_state"]
        self.last_resume_time = None
        del self._restore_state
