"""时间轴对齐服务 Python 调用示例。

用法：先启动服务（python -m app 或 docker compose up），再运行：
    python3 examples/client.py [base_url]
默认连接 http://127.0.0.1:8000
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


class TimelineClient:
    """时间轴对齐服务的极简客户端（仅标准库）。"""

    def __init__(self, base: str = "http://127.0.0.1:8000"):
        self.base = base.rstrip("/")

    def _call(self, method: str, path: str, payload: dict | None = None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    # 设备
    def register_device(self, device_id: str, name: str | None = None):
        payload = {"device_id": device_id}
        if name is not None:
            payload["name"] = name
        return self._call("POST", "/devices", payload)

    # 时钟周期（设备重启轮次）
    def register_cycle(self, device_id: str, cycle_id: str, note: str | None = None):
        payload = {"cycle_id": cycle_id}
        if note is not None:
            payload["note"] = note
        return self._call("POST", f"/devices/{device_id}/cycles", payload)

    def list_cycles(self, device_id: str):
        return self._call("GET", f"/devices/{device_id}/cycles")

    # 锚点
    def add_anchor(self, device_id: str, local_ms: int, utc_ms: int,
                   cycle_id: str | None = None):
        payload = {"local_ms": local_ms, "utc_ms": utc_ms}
        if cycle_id is not None:
            payload["cycle_id"] = cycle_id
        return self._call("POST", f"/devices/{device_id}/anchors", payload)

    def replace_anchors(self, device_id: str, anchors: list[dict],
                        cycle_id: str | None = None):
        payload = {"anchors": anchors}
        if cycle_id is not None:
            payload["cycle_id"] = cycle_id
        return self._call("PUT", f"/devices/{device_id}/anchors", payload)

    # 声段
    def submit_segment(self, capture_id: str, device_id: str,
                       local_start_ms: int, local_end_ms: int,
                       note: str | None = None, cycle_id: str | None = None):
        payload = {"capture_id": capture_id, "device_id": device_id,
                   "local_start_ms": local_start_ms, "local_end_ms": local_end_ms}
        if note is not None:
            payload["note"] = note
        if cycle_id is not None:
            payload["cycle_id"] = cycle_id
        return self._call("POST", "/segments", payload)

    # 时间轴与版本
    def timeline(self):
        return self._call("GET", "/timeline/current")

    def publish(self, note: str | None = None):
        return self._call("POST", "/versions", {"note": note} if note else None)

    def version(self, version_no: int):
        return self._call("GET", f"/versions/{version_no}")


def show(title: str, status: int, body: dict) -> None:
    print(f"\n=== {title} (HTTP {status}) ===")
    print(json.dumps(body, ensure_ascii=False, indent=2)[:1200])


def main() -> None:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    cli = TimelineClient(base)

    # 1. 注册两台录音设备
    show("注册设备 D1", *cli.register_device("D1", "录音笔-甲"))
    show("注册设备 D2", *cli.register_device("D2", "录音笔-乙"))

    # 2. 录入同步锚点（设备本地毫秒 <-> UTC 毫秒）
    #    D1：本地 1000ms 对时到 UTC 1700000000000，本地 61000ms 对时到 1700000060500
    #        （设备时钟每 1000ms 快约 8.3ms，线性漂移）
    show("D1 锚点#1", *cli.add_anchor("D1", 1000, 1700000000000))
    show("D1 锚点#2", *cli.add_anchor("D1", 61000, 1700000060500))
    show("D2 锚点#1", *cli.add_anchor("D2", 0, 1699999997000))
    show("D2 锚点#2", *cli.add_anchor("D2", 60000, 1700000057000))

    # 3. 提交声段（唯一采集编号 + 设备 + 本地起止毫秒）
    show("提交声段 CAP-0001", *cli.submit_segment("CAP-0001", "D1", 5000, 20000, note="鸟鸣"))
    show("提交声段 CAP-0002", *cli.submit_segment("CAP-0002", "D2", 3000, 9000))
    show("提交声段 CAP-0003(越界->无法定位)",
         *cli.submit_segment("CAP-0003", "D1", 90000, 95000))

    # 4. 幂等重传与冲突
    show("CAP-0001 同内容重传(幂等)",
         *cli.submit_segment("CAP-0001", "D1", 5000, 20000, note="鸟鸣"))
    show("CAP-0001 异内容重传(409 冲突)",
         *cli.submit_segment("CAP-0001", "D1", 5000, 20001, note="鸟鸣"))

    # 5. 发布版本 v1
    show("发布 v1", *cli.publish("初始对齐"))

    # 6. 锚点更正：D1 的 UTC 基准整体修正 +250ms，声段自动重算
    show("更正 D1 锚点", *cli.replace_anchors("D1", [
        {"local_ms": 1000, "utc_ms": 1700000000250},
        {"local_ms": 61000, "utc_ms": 1700000060750},
    ]))

    # 7. 发布 v2：changes 中可见 CAP-0001 的位移
    show("发布 v2", *cli.publish("修正 D1 对时"))

    # 8. 回看 v1：已发布版本保持不变
    show("回看 v1（不变）", *cli.version(1))

    # 9. 设备重启：本地毫秒归零，登记新周期 boot-2 并复用 local_ms
    show("登记周期 boot-2", *cli.register_cycle("D1", "boot-2", note="第二次开机"))
    show("周期列表", *cli.list_cycles("D1"))
    #    boot-2 周期在 local_ms=1000 处重新对时（UTC 与 default 完全不同）
    show("boot-2 锚点#1（复用 local_ms=1000）",
         *cli.add_anchor("D1", 1000, 1700010000000, cycle_id="boot-2"))
    show("boot-2 锚点#2",
         *cli.add_anchor("D1", 61000, 1700010060500, cycle_id="boot-2"))
    #    相同 local 起止的声段，只按 boot-2 周期锚点映射
    show("提交 boot-2 声段 CAP-0100",
         *cli.submit_segment("CAP-0100", "D1", 5000, 20000,
                             note="重启后录制", cycle_id="boot-2"))
    #    同采集编号换周期 -> 409（周期计入内容指纹）
    show("CAP-0001 换周期重传(409)",
         *cli.submit_segment("CAP-0001", "D1", 5000, 20000,
                             note="鸟鸣", cycle_id="boot-2"))

    # 10. 发布 v3：各周期锚点与声段投影原子冻结，文档带 cycle_id / format_rev=2
    show("发布 v3（双周期冻结）", *cli.publish("加入重启周期"))


if __name__ == "__main__":
    main()
