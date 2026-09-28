"""工业设备数字孪生第一版：带缓冲位的虚拟装配线事件框架。

模块导出统一的事件约定，后续更复杂设备只需实现 ``on_event``/``snapshot``/
``restore`` 并向调度器登记命名事件即可接入同一条仿真主线。
"""

from .clock import Clock
from .events import Scheduler
from .material import Material
from .buffer import Buffer
from .station import Station, FailureModel
from .line import AssemblyLine

__all__ = [
    "Clock",
    "Scheduler",
    "Material",
    "Buffer",
    "Station",
    "FailureModel",
    "AssemblyLine",
]
