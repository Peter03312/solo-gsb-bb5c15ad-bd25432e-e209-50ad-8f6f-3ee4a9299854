#!/usr/bin/env bash
# 时间轴对齐服务 curl 调用示例。
# 用法：先启动服务，再执行 bash examples/demo.sh [base_url]
set -euo pipefail

BASE="${1:-http://127.0.0.1:8000}"
pp() { python3 -m json.tool --no-ensure-ascii 2>/dev/null || cat; }

echo "== 健康检查 =="
curl -s "$BASE/health" | pp

echo; echo "== 注册设备 =="
curl -s -X POST "$BASE/devices" -H 'Content-Type: application/json' \
  -d '{"device_id":"D1","name":"录音笔-甲"}' | pp
curl -s -X POST "$BASE/devices" -H 'Content-Type: application/json' \
  -d '{"device_id":"D2","name":"录音笔-乙"}' | pp

echo; echo "== 录入同步锚点（本地毫秒 <-> UTC 毫秒）=="
curl -s -X POST "$BASE/devices/D1/anchors" -H 'Content-Type: application/json' \
  -d '{"local_ms":1000,"utc_ms":1700000000000}' | pp
curl -s -X POST "$BASE/devices/D1/anchors" -H 'Content-Type: application/json' \
  -d '{"local_ms":61000,"utc_ms":1700000060500}' | pp
curl -s -X POST "$BASE/devices/D2/anchors" -H 'Content-Type: application/json' \
  -d '{"local_ms":0,"utc_ms":1699999997000}' | pp
curl -s -X POST "$BASE/devices/D2/anchors" -H 'Content-Type: application/json' \
  -d '{"local_ms":60000,"utc_ms":1700000057000}' | pp

echo; echo "== 锚点 UTC 不递增 -> 400 拒绝 =="
curl -s -X POST "$BASE/devices/D1/anchors" -H 'Content-Type: application/json' \
  -d '{"local_ms":30000,"utc_ms":1700000000000}' | pp

echo; echo "== 提交声段 =="
curl -s -X POST "$BASE/segments" -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0001","device_id":"D1","local_start_ms":5000,"local_end_ms":20000,"note":"鸟鸣"}' | pp
curl -s -X POST "$BASE/segments" -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0002","device_id":"D2","local_start_ms":3000,"local_end_ms":9000}' | pp
echo "-- 越界声段 -> 无法定位 --"
curl -s -X POST "$BASE/segments" -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0003","device_id":"D1","local_start_ms":90000,"local_end_ms":95000}' | pp

echo; echo "== 同编号同内容重传 -> 幂等 =="
curl -s -X POST "$BASE/segments" -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0001","device_id":"D1","local_start_ms":5000,"local_end_ms":20000,"note":"鸟鸣"}' | pp

echo; echo "== 同编号异内容 -> 409 冲突 =="
curl -s -X POST "$BASE/segments" -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0001","device_id":"D1","local_start_ms":5000,"local_end_ms":20001}' | pp

echo; echo "== 起止倒置 -> 400 拒绝 =="
curl -s -X POST "$BASE/segments" -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0004","device_id":"D1","local_start_ms":9000,"local_end_ms":1000}' | pp

echo; echo "== 当前时间轴（按 UTC 起点、设备、采集编号排序）=="
curl -s "$BASE/timeline/current" | pp

echo; echo "== 发布版本 v1 =="
curl -s -X POST "$BASE/versions" -H 'Content-Type: application/json' -d '{"note":"初始对齐"}' | pp

echo; echo "== 锚点更正（D1 UTC 基准 +250ms，整体替换）=="
curl -s -X PUT "$BASE/devices/D1/anchors" -H 'Content-Type: application/json' \
  -d '{"anchors":[{"local_ms":1000,"utc_ms":1700000000250},{"local_ms":61000,"utc_ms":1700000060750}]}' | pp

echo; echo "== 发布版本 v2（changes 中含位移）=="
curl -s -X POST "$BASE/versions" | pp

echo; echo "== 回看 v1：已发布版本保持不变 =="
curl -s "$BASE/versions/1" | pp

echo; echo "== 版本列表 =="
curl -s "$BASE/versions" | pp

# ----------------------------------------------------------------------
# 时钟周期（cycle）：设备重启后本地毫秒归零，可登记新周期复用 local_ms
# ----------------------------------------------------------------------

echo; echo "== 登记新周期（设备 D2 重启后的新一轮，本地毫秒重新从 0 起算）=="
curl -s -X POST "$BASE/devices/D2/cycles" -H 'Content-Type: application/json' \
  -d '{"cycle_id":"boot-2","note":"第二次开机"}' | pp
echo "-- 周期列表（注册设备时自动带 default 周期）--"
curl -s "$BASE/devices/D2/cycles" | pp

echo; echo "== 新周期录入锚点：local_ms 与 default 周期可重复 =="
curl -s -X POST "$BASE/devices/D2/anchors" -H 'Content-Type: application/json' \
  -d '{"cycle_id":"boot-2","local_ms":0,"utc_ms":1700010000000}' | pp
curl -s -X POST "$BASE/devices/D2/anchors" -H 'Content-Type: application/json' \
  -d '{"cycle_id":"boot-2","local_ms":60000,"utc_ms":1700010060000}' | pp

echo; echo "== 同设备同 local_ms 在两个周期各有一个锚点 =="
curl -s "$BASE/devices/D2/anchors" | pp
echo "-- 只看某一周期：?cycle_id=boot-2 --"
curl -s "$BASE/devices/D2/anchors?cycle_id=boot-2" | pp

echo; echo "== 声段指定周期：相同 local 起止，只用所属周期锚点映射 =="
curl -s -X POST "$BASE/segments" -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0101","device_id":"D2","cycle_id":"boot-2","local_start_ms":3000,"local_end_ms":9000,"note":"重启后录制"}' | pp

echo; echo "== 同一采集编号换周期重传 -> 409（周期计入内容）=="
curl -s -X POST "$BASE/segments" -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0002","device_id":"D2","cycle_id":"boot-2","local_start_ms":3000,"local_end_ms":9000}' | pp

echo; echo "== 周期不存在 -> 404 cycle_not_found，且不写入 =="
curl -s -X POST "$BASE/segments" -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-9999","device_id":"D2","cycle_id":"not-registered","local_start_ms":1,"local_end_ms":2}' | pp

echo; echo "== 当前时间轴（cycles/anchors/segments 均带周期归属）=="
curl -s "$BASE/timeline/current" | pp

echo; echo "== 发布 v3：原子冻结各周期锚点与声段投影（format_rev=2）=="
curl -s -X POST "$BASE/versions" -H 'Content-Type: application/json' -d '{"note":"加入重启周期"}' | pp

echo; echo "== 只更正 boot-2 周期锚点（default 等其他周期不动）=="
curl -s -X PUT "$BASE/devices/D2/anchors" -H 'Content-Type: application/json' \
  -d '{"cycle_id":"boot-2","anchors":[{"local_ms":0,"utc_ms":1700010000300},{"local_ms":60000,"utc_ms":1700010060300}]}' | pp

echo; echo "== 发布 v4：changes 仅列出 boot-2 周期声段位移 =="
curl -s -X POST "$BASE/versions" | pp

echo; echo "== 回看 v3：各周期冻结不变 =="
curl -s "$BASE/versions/3" | pp
