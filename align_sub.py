#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""align_sub.py

输入: subs.csv / usage.csv（与脚本同目录，可用参数覆盖）
输出: segments.csv / usage_ledger.csv / invalid_usage.csv / sub_report.txt / sub_notes.txt

规则要点（详见生成出来的 sub_notes.txt）:
  1. 同一事件号 / 使用号出现多次视为平台的修正重发，以“到达顺序靠后的那一份”为准（后到覆盖先到），
     因此改过生效时刻或档位的重发也会被正确采纳。
  2. 段按会员、按生效时刻（不是到达顺序）排列；每个事件开启一段，退订段 plan=none，
     to_ts 为下一事件的生效时刻，末段 to_ts 留空。
  3. 区间为左闭右开 [from_ts, to_ts):恰落在切换时刻的使用归新段。
  4. 开通前(首个事件之前)的使用记 before_join;落在退订空窗(none 段)内的使用记 gap;
     二者都算无效,不进 usage_ledger。
"""

import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

ACTION_JOIN = ("开通", "复订")
ACTION_UNSUB = "退订"
PLANS = ("basic", "silver", "gold")


def read_rows(path):
    """读 csv（容忍 BOM / 空行），返回 [dict, ...]，保持文件原顺序。"""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        out = []
        for row in reader:
            if not row:
                continue
            vals = ["" if v is None else v.strip() for v in row.values()]
            if not any(vals):
                continue
            out.append({k.strip(): ("" if v is None else v.strip())
                        for k, v in row.items()})
        return out


def dedupe(rows, id_field):
    """按编号去重：后到的（行序靠后的）覆盖先到的。返回 (唯一行列表, 被重发覆盖的编号集合)。

    唯一行列表保持“最后一份”的到达顺序，便于后续稳定排序。
    """
    last = {}
    resent = set()
    for idx, row in enumerate(rows):
        key = row[id_field]
        if key in last:
            resent.add(key)
        last[key] = (idx, row)
    ordered = [row for _, row in sorted(last.values(), key=lambda p: p[0])]
    return ordered, resent


def build_segments(subs):
    """按会员 + 生效时刻排事件，拉成段。

    返回 {member: [(from_ts, to_ts, plan), ...]}, to_ts 为 '' 表示末段。
    """
    by_member = {}
    for row in subs:
        by_member.setdefault(row["会员"], []).append(row)

    segments = {}
    for member, events in by_member.items():
        # 生效时刻升序；同一时刻理论上不会出现，若出现按动作先后仍保证确定性。
        events.sort(key=lambda r: (r["生效时刻"], r["事件号"]))
        segs = []
        for i, ev in enumerate(events):
            plan = "none" if ev["动作"] == ACTION_UNSUB else ev["档位"]
            to_ts = events[i + 1]["生效时刻"] if i + 1 < len(events) else ""
            segs.append((ev["生效时刻"], to_ts, plan))
        segments[member] = segs
    return segments


def locate(segs, used_at):
    """在段列表里定位 used_at 所在段。左闭右开。

    返回 (plan, status)，status ∈ {'ok', 'before_join', 'gap'}。
    """
    if not segs or used_at < segs[0][0]:
        return None, "before_join"
    # 段按 from_ts 升序，找到最后一个 from_ts <= used_at 的段
    lo, hi = 0, len(segs) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if segs[mid][0] <= used_at:
            lo = mid
        else:
            hi = mid - 1
    plan = segs[lo][2]
    if plan == "none":
        return plan, "gap"
    return plan, "ok"


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else HERE
    subs_path = os.path.join(base, "subs.csv")
    usage_path = os.path.join(base, "usage.csv")

    subs_raw = read_rows(subs_path)
    usage_raw = read_rows(usage_path)

    subs, subs_resent = dedupe(subs_raw, "事件号")
    usage, usage_resent = dedupe(usage_raw, "使用号")

    segments = build_segments(subs)

    # ---- 段表 ----
    seg_rows = []
    for member in sorted(segments):
        for from_ts, to_ts, plan in segments[member]:
            seg_rows.append((member, plan, from_ts, to_ts))

    # ---- 使用分账 ----
    ledger = []
    invalid = []
    counts = {"basic": 0, "silver": 0, "gold": 0}
    for row in sorted(usage, key=lambda r: r["使用号"]):
        uid, member, benefit, used_at = (row["使用号"], row["会员"],
                                         row["权益"], row["使用时刻"])
        plan, status = locate(segments.get(member, []), used_at)
        if status == "ok":
            ledger.append((uid, member, benefit, used_at, plan))
            counts[plan] += 1
        else:
            invalid.append((uid, member, benefit, used_at, status))

    # ---- 对账 ----
    assert len(seg_rows) == len(subs), (
        "段数 %d != subs 终值事件数 %d" % (len(seg_rows), len(subs)))
    assert len(ledger) + len(invalid) == len(usage), (
        "有效+无效 %d != usage 终值行数 %d"
        % (len(ledger) + len(invalid), len(usage)))

    resends = len(subs_resent) + len(usage_resent)

    # ---- 写出（全部确定性排序） ----
    def write(path, header, rows):
        with open(os.path.join(base, path), "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(header)
            w.writerows(rows)

    write("segments.csv", ["member", "plan", "from_ts", "to_ts"], seg_rows)
    write("usage_ledger.csv",
          ["usage_id", "member", "benefit", "used_at", "plan"], ledger)
    write("invalid_usage.csv",
          ["usage_id", "member", "benefit", "used_at", "reason"], invalid)

    with open(os.path.join(base, "sub_report.txt"), "w", encoding="utf-8",
              newline="") as f:
        f.write("basic %d\n" % counts["basic"])
        f.write("silver %d\n" % counts["silver"])
        f.write("gold %d\n" % counts["gold"])
        f.write("invalid %d\n" % len(invalid))
        f.write("resends %d\n" % resends)

    notes = """align_sub.py 处理口径说明

一、重发（修正重发）
subs.csv 中同一事件号、usage.csv 中同一使用号出现两次及以上，视为平台对同一条记录的修正重发。
处理：按编号去重，保留到达顺序中最后出现的那一份（后到覆盖先到）。这样若重发修改了生效时刻或档位，
以最终那份为准；文件行序即到达先后，与生效先后无关，去重后再按生效时刻排序。
resends 计的是“被重发覆盖的编号个数”：subs 中被重发的不同事件号数 + usage 中被重发的不同使用号数，
同一编号不论重发几次只计 1。

二、段（segments.csv）
每个会员的事件按“生效时刻”升序排列（不是文件到达顺序），每个事件开启一段：
  - 开通 / 复订 / 升档 / 降档：档位一律取该事件行记录的档位（升档行可能记成更低的档位，
    以文件记录为准，不做合理性推断）；复订视为重新开通，其档位即复订后的档位。
  - 退订：开一段，plan 记 none。
from_ts 取该事件的生效时刻；to_ts 取下一事件的生效时刻；末段 to_ts 留空。
因此段数 = subs 去重后的终值事件数，逐会员逐段森林式展开。

三、区间边界
段定义为左闭右开 [from_ts, to_ts)。使用时刻恰等于某次档位切换/退订/复订的生效时刻时，
归入该时刻开始的新段（即“切换那一刻起，新档位生效”）。开通前(首段 from_ts 之前)的使用单独处理，见下。
末段 to_ts 为空，表示该段一直延续到数据末尾之后，落在其后任意时刻的使用仍归末段、按其 plan 计。

四、有效与无效使用的判定
usage_ledger.csv 只收有效使用，plan 记使用时刻所处段的档位。以下两种计无效，写入 invalid_usage.csv：
  - before_join：使用时刻早于该会员首个事件的生效时刻（开通之前就有使用）。
  - gap：使用时刻落在 plan=none 的退订段内（退订生效后、复订生效前的空窗期）。
退订事件比生效时刻晚到、期间会员照常使用权益的情况，一律以“生效时刻”判断，即这些使用落在 none 段，
记 gap 无效——重发修正过的生效时刻会移动段边界，从而改变被判定的使用归属，这正是以终值事件为准的意义。
若某会员末段为 none（退订后未复订），其后所有使用均为 gap。

五、编号与排序
usage_ledger.csv、invalid_usage.csv 均按 usage_id 升序；segments.csv 按 member、from_ts 升序。
脚本不含任何时间戳/随机量，重复执行产物逐字节一致。

六、对账
segments.csv 段数 == subs 去重后终值事件数；usage_ledger 行数 + invalid_usage 行数 == usage 去重后终值行数。
两项在脚本内以断言校验，不通过则报错退出。
"""
    with open(os.path.join(base, "sub_notes.txt"), "w", encoding="utf-8",
              newline="") as f:
        f.write(notes)


if __name__ == "__main__":
    main()
