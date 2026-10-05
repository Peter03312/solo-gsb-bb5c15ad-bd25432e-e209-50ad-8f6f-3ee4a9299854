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

    def cycle(self, device_id: str, cycle_id: str):
        """周期详情：封存状态、稳定指纹、不可定位声段清单。"""
        return self._call("GET", f"/devices/{device_id}/cycles/{cycle_id}")

    def seal_cycle(self, device_id: str, cycle_id: str, fingerprint: str):
        """封存周期：携带 GET 周期详情返回的 fingerprint（单事务核对内容）。"""
        return self._call("POST", f"/devices/{device_id}/cycles/{cycle_id}/seal",
                          {"fingerprint": fingerprint})

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

    def delete_segment(self, capture_id: str):
        return self._call("DELETE", f"/segments/{capture_id}")

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

    # 11. 现场核对完成后封存周期：先取周期详情（稳定指纹/封存状态/可定位性）
    #     D1 的 default 周期含越界声段 CAP-0003（unlocatable）-> 封存被拒
    status, detail_d1 = cli.cycle("D1", "default")
    show("D1 default 周期详情（含不可定位声段）", status, detail_d1)
    show("封存 D1 default（409：存在不可定位声段）",
         *cli.seal_cycle("D1", "default", detail_d1["cycle"]["fingerprint"]))

    #     改封存 D1 的 boot-2 周期（CAP-0100 可定位）
    _, detail = cli.cycle("D1", "boot-2")
    stale_fp = detail["cycle"]["fingerprint"]
    # 晚到一条锚点 -> 旧指纹过期
    show("封存前补录锚点（内容变化，指纹随之变化）",
         *cli.add_anchor("D1", 30000, 1700010030000, cycle_id="boot-2"))
    show("携带过期指纹封存（409 fingerprint_mismatch，状态不变）",
         *cli.seal_cycle("D1", "boot-2", stale_fp))

    # 12. 重新核对、取最新指纹后封存生效：返回封存时间与最终指纹
    _, detail = cli.cycle("D1", "boot-2")
    show("封存 boot-2 周期（201）",
         *cli.seal_cycle("D1", "boot-2", detail["cycle"]["fingerprint"]))
    fp = detail["cycle"]["fingerprint"]

    # 13. 重复相同封存请求 -> 返回首次结果（200 幂等，sealed_at 与首次一致）
    show("重复相同封存请求(幂等)", *cli.seal_cycle("D1", "boot-2", fp))

    # 14. 封存后晚到记录一律拒绝（409 cycle_sealed，封存不可撤销）
    show("封存后补锚点(409)",
         *cli.add_anchor("D1", 40000, 1700010040000, cycle_id="boot-2"))
    show("封存后删声段(409)", *cli.delete_segment("CAP-0100"))
    show("封存后更正锚点(409，涉及封存周期即整体拒绝)",
         *cli.replace_anchors("D1", [
             {"cycle_id": "boot-2", "local_ms": 1000, "utc_ms": 1700010000999},
             {"cycle_id": "boot-2", "local_ms": 61000, "utc_ms": 1700010060999},
         ]))

    # 15. 同内容声段重传仍幂等、异内容仍冲突（既有规则不变，均不产生写入）
    show("同内容重传(仍 200 幂等)",
         *cli.submit_segment("CAP-0100", "D1", 5000, 20000,
                             note="重启后录制", cycle_id="boot-2"))
    show("异内容重传(仍 409 冲突)",
         *cli.submit_segment("CAP-0100", "D1", 5000, 20001,
                             note="重启后录制", cycle_id="boot-2"))


if __name__ == "__main__":
    main()
