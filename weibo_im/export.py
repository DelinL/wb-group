"""成员发言导出 — 用于 AI 分析的数据导出模块

纯读操作：不修改任何表数据，不依赖 Cookie，不涉及网络请求。
供 crawl.py CLI 或程序化调用。
"""
from __future__ import annotations

import csv
import json
import logging
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from .db import set_db_path, init_db, get_conn, get_group_list

CST = timezone(timedelta(hours=8))
log = logging.getLogger("weibo_im.export")

# 导出文件默认写入系统临时目录，避免污染项目目录。
# 子目录名前缀 weibo_export_ 便于识别与批量清理。
_EXPORT_DIR = Path(tempfile.gettempdir()) / "weibo_export"


# ── 工具函数 ────────────────────────────────────────────────


def ms_to_cst(ms: int | None) -> str | None:
    """毫秒时间戳转 CST 格式化字符串；空则返回 None。"""
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=CST).strftime("%Y-%m-%d %H:%M:%S")


# ── 数据库查询（只读）───────────────────────────────────────


def get_members(gid: int) -> list[dict]:
    """返回群内发过言的成员列表（sender_id + sender_name），按最近发言倒序。

    沿用 server.py query_members_list 的口径：仅统计 msg_type IN (100, 321) 的
    真实用户发言，GROUP BY sender_id，给出 msg_count 与最近发言时间，便于定位
    准确昵称/ID。
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT sender_id, sender_name, MAX(created_at) AS last_msg_at, "
        "COUNT(*) AS msg_count FROM messages "
        "WHERE gid=? AND msg_type IN (100, 321) "
        "GROUP BY sender_id ORDER BY last_msg_at DESC",
        (gid,),
    ).fetchall()
    return [{"sender_id": r["sender_id"],
             "sender_name": r["sender_name"],
             "msg_count": r["msg_count"],
             "last_msg_at": r["last_msg_at"]} for r in rows]


def get_member_messages(
    gid: int,
    sender_id: int | None = None,
    sender_name: str | None = None,
    since_ms: int | None = None,
    until_ms: int | None = None,
    msg_types: tuple[int, ...] = (100, 321),
) -> list[dict]:
    """返回指定群内某成员本人发言（默认普通消息 321 + 微博分享 100），按时间升序。

    - sender_id 与 sender_name 可任传其一或同时传入，按传入情况拼接 WHERE；
      优先用 sender_id（稳定），sender_name 便于按可感知昵称导出。
    - since_ms/until_ms 为 created_at 毫秒范围：since 闭区间 (>=)、until 开区间 (<)，
      与项目内 CST 日期区间 [start, end) 约定一致。
    - msg_types 默认 (100, 321)；传空元组则不加该过滤（一般不必要）。

    返回每行 dict（含 mid/gid/msg_type/msg_type_name/media_type/sender_id/
    sender_name/text/fid/media_orig_url/url_objects/created_at/group_name），
    按 created_at ASC 排列，契合按时间线的 AI 序列分析。
    """
    conn = get_conn()
    conds = ["gid=?"]
    params: list[Any] = [gid]
    if msg_types:
        placeholders = ",".join("?" for _ in msg_types)
        conds.append(f"msg_type IN ({placeholders})")
        params.extend(msg_types)
    if sender_id is not None:
        conds.append("sender_id=?")
        params.append(sender_id)
    if sender_name:
        conds.append("sender_name=?")
        params.append(sender_name)
    if since_ms is not None:
        conds.append("created_at>=?")
        params.append(since_ms)
    if until_ms is not None:
        conds.append("created_at<?")
        params.append(until_ms)
    sql = (
        "SELECT mid, gid, msg_type, msg_type_name, media_type, "
        "sender_id, sender_name, text, fid, media_orig_url, "
        "url_objects, created_at, group_name "
        "FROM messages WHERE " + " AND ".join(conds) +
        " ORDER BY created_at ASC"
    )
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ── JSON 组装 ───────────────────────────────────────────────


def build_export_json(rows: list[dict]) -> list[dict]:
    """将查询结果组装为适合 AI 分析的导出数据。

    返回消息列表，每条仅含 sender_name（发言者昵称）与 text（正文）两项，
    按 created_at ASC 排列，契合按时间线的 AI 序列分析。
    """
    return [
        {
            "sender_name": r.get("sender_name") or "",
            "text": r.get("text") or "",
        }
        for r in rows
    ]


# ── 高级编排（含 DB 初始化 + 文件写入）─────────────────────


def export_member_messages(
    db_path: str,
    gid: int,
    sender_name: str | None = None,
    sender_id: int | None = None,
    since_ms: int | None = None,
    until_ms: int | None = None,
    output_path: str | None = None,
    compact: bool = False,
    fmt: str = "json",
) -> dict:
    """导出指定群内某成员本人发言为文件（JSON 或 CSV）。

    完整流程：初始化 DB 连接 → 查询消息 → 查询群名 → 组装数据 → 写入文件。

    fmt="json"（默认）：输出纯 JSON 数组，每条含 sender_name / text 两项。
        compact=True 时进一步输出紧凑（minified）JSON（去缩进、去空格），
        显著减小体积，适合直接喂大模型或网络传输；否则保留 2 空格缩进便于阅读。
    fmt="csv"：输出 CSV 文件，表头为 sender_name,text（UTF-8-SIG 编码，
        Excel 可直接打开且中文不乱码）；compact 参数对 CSV 无影响。

    返回结果字典:
        {"path": str,          # 输出文件路径
         "count": int,         # 导出条数
         "start_cst": str,     # 最早消息时间
         "end_cst": str,       # 最晚消息时间
         "group_name": str}    # 群名称
    """
    set_db_path(db_path)
    init_db()

    sid = sender_id if sender_id else None
    sname = sender_name or None

    rows = get_member_messages(
        gid=gid,
        sender_id=sid,
        sender_name=sname,
        since_ms=since_ms,
        until_ms=until_ms,
    )

    # 过滤系统噪声（粉丝群提示、纯 URL 网页分享、"快来围观吧"自动提示）
    total_before = len(rows)
    rows = [r for r in rows if not _is_noise_message(r)]
    filtered_count = total_before - len(rows)
    if filtered_count:
        log.info("过滤 %d 条噪声消息（粉丝群/纯URL/快来围观吧）", filtered_count)

    # 查群名称
    group_name = ""
    for g in get_group_list():
        if g["gid"] == gid:
            group_name = g.get("name", "")
            break

    msgs = build_export_json(rows)

    # 确定输出路径（CSV 用 .csv 扩展名）
    if not output_path:
        sender_slug = sname or (str(sid) if sid else "member")
        safe = "".join(c for c in sender_slug if c.isalnum() or c in "._-") or "member"
        ext = "csv" if fmt == "csv" else "json"
        _EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = str(_EXPORT_DIR / f"export_{gid}_{safe}.{ext}")

    if fmt == "csv":
        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["sender_name", "text"])
            writer.writeheader()
            for m in msgs:
                writer.writerow(m)
    else:
        with open(output_path, "w", encoding="utf-8") as f:
            if compact:
                json.dump(msgs, f, ensure_ascii=False, separators=(",", ":"))
            else:
                json.dump(msgs, f, ensure_ascii=False, indent=2)

    start_cst = ms_to_cst(rows[0].get("created_at")) if rows else ""
    end_cst = ms_to_cst(rows[-1].get("created_at")) if rows else ""

    if filtered_count:
        log.info(
            "已导出 %d 条发言（过滤噪声 %d 条）→ %s",
            len(msgs), filtered_count, output_path,
        )
    else:
        log.info("已导出 %d 条发言 → %s", len(msgs), output_path)

    return {
        "path": output_path,
        "count": len(msgs),
        "start_cst": start_cst,
        "end_cst": end_cst,
        "group_name": group_name,
    }


def list_members(db_path: str, gid: int) -> list[dict]:
    """列出指定群内发过言的成员信息。

    初始化 DB → 查询成员列表 → 返回 dict 列表。
    每条含 sender_id / sender_name / msg_count / last_msg_at。
    """
    set_db_path(db_path)
    init_db()
    members = get_members(gid)
    if not members:
        log.info("该群还没有任何用户发言记录")
    else:
        log.info("群 %d 的发言成员（%d 人）:", gid, len(members))
        for m in members:
            ts = m.get("last_msg_at") or 0
            last = ms_to_cst(ts) or "(无)"
            log.info("  sender_id=%-12d 发言=%-5d 最近=%s  %s",
                     m["sender_id"], m["msg_count"], last, m["sender_name"] or "(未知)")
    return members


# ── 每日群聊日报导出 ───────────────────────────────────────


def _resolve_target_sender_id(
    gid: int,
    sender_name: str | None,
    sender_id: int | None,
) -> int | None:
    """解析目标成员的 sender_id（用于 is_target 标记）。

    - sender_id 已提供 → 直接返回。
    - 仅 sender_name → 查该群内该昵称最近一条发言的 sender_id（取最新值，避免改名漂移）。
    - 都未提供 → 返回 None（is_target 全为 0，仅导出当日群聊上下文）。
    """
    if sender_id:
        return int(sender_id)
    if not sender_name:
        return None
    conn = get_conn()
    row = conn.execute(
        "SELECT sender_id FROM messages "
        "WHERE gid=? AND sender_name=? AND msg_type IN (100, 321) "
        "ORDER BY created_at DESC LIMIT 1",
        (gid, sender_name),
    ).fetchone()
    return int(row["sender_id"]) if row else None


def _parse_date_to_day_range(date_str: str) -> tuple[int, int]:
    """将日期字符串解析为 CST 当日 [start_ms, end_ms) 毫秒区间。

    支持 "YYYY-MM-DD" 和 "YYYY/MM/DD" 两种格式。返回 (当日零点ms, 次日零点ms)。
    """
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(date_str, fmt)
        except ValueError:
            continue
        start_dt = dt.replace(tzinfo=CST)
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = start_ms + 86400000
        return start_ms, end_ms
    raise ValueError(
        f"无法解析日期: {date_str}（支持格式 YYYY-MM-DD 或 YYYY/MM/DD）"
    )


def get_daily_context_messages(
    gid: int,
    date_str: str,
) -> list[dict]:
    """返回指定群当日全部用户发言（msg_type 100/321），按时间升序。

    作为日报导出的数据源：包含目标成员当日发言 + 其他成员当日发言（上下文）。
    每行 dict 含 mid/created_at/sender_id/sender_name/text/group_name。
    """
    start_ms, end_ms = _parse_date_to_day_range(date_str)
    conn = get_conn()
    rows = conn.execute(
        "SELECT mid, created_at, sender_id, sender_name, text, group_name "
        "FROM messages "
        "WHERE gid=? AND created_at>=? AND created_at<? "
        "AND msg_type IN (100, 321) "
        "ORDER BY created_at ASC",
        (gid, start_ms, end_ms),
    ).fetchall()
    return [dict(r) for r in rows]


def _is_noise_message(row: dict) -> bool:
    """判断消息是否为应剔除的系统噪声。

    规则：
    - sender_name == "粉丝群" → 系统自动提示（如"恭喜…获得标识"、"涉及资金问题请务必提高警惕"等）
    - text 去首尾空白后以 http:// 或 https:// 开头 → 纯 URL 网页分享（微博分享自动生成的纯链接）
    - text 包含 "快来围观吧" → 微博分享时系统自动生成的提示（"我发布了新微博，快来围观吧！"）

    返回 True 表示该条消息应被剔除。
    """
    sender_name = (row.get("sender_name") or "").strip()
    if sender_name == "粉丝群":
        return True
    text = (row.get("text") or "").strip()
    if not text:
        return False
    if text.startswith(("http://", "https://")):
        return True
    if "快来围观吧" in text:
        return True
    return False


def export_daily_report(
    db_path: str,
    gid: int,
    date_str: str,
    sender_name: str | None = None,
    sender_id: int | None = None,
    output_path: str | None = None,
) -> dict:
    """导出指定群+成员+日期的当日群聊上下文 CSV（用于 AI 生成成员日报）。

    完整流程：初始化 DB → 解析目标 sender_id → 查询当日全部用户发言 →
    标记 is_target → 查群名 → 写 CSV（utf-8-sig）。

    CSV 四列：created_at(CST字符串), sender_name, text, is_target(1/0)。

    返回结果字典:
        {"path": str,           # 输出文件路径
         "count": int,          # 当日消息总数
         "target_count": int,   # 目标成员发言数
         "context_count": int,  # 其他成员发言数（上下文）
         "date": str,           # 日期字符串
         "group_name": str,     # 群名
         "target_sender_id": int|None,  # 目标成员 sender_id
         "target_sender_name": str}     # 目标成员昵称（输入或由 sender_id 反查）
    """
    set_db_path(db_path)
    init_db()

    target_sid = _resolve_target_sender_id(gid, sender_name, sender_id)

    rows = get_daily_context_messages(gid, date_str)

    # 目标昵称：优先用输入；若仅传 sender_id 则反查最新昵称
    target_sname = sender_name or ""
    if not target_sname and target_sid:
        for r in rows:
            if r["sender_id"] == target_sid:
                target_sname = r.get("sender_name") or ""
                break
        if not target_sname:
            # 当日无发言，回退查历史最新昵称
            conn = get_conn()
            row = conn.execute(
                "SELECT sender_name FROM messages "
                "WHERE gid=? AND sender_id=? AND msg_type IN (100, 321) "
                "ORDER BY created_at DESC LIMIT 1",
                (gid, target_sid),
            ).fetchone()
            target_sname = row["sender_name"] if row else ""

    # 过滤系统噪声（粉丝群提示、纯 URL 网页分享、"快来围观吧"自动提示）
    total_before = len(rows)
    rows = [r for r in rows if not _is_noise_message(r)]
    filtered_count = total_before - len(rows)
    if filtered_count:
        log.info("过滤 %d 条噪声消息（粉丝群/纯URL/快来围观吧）", filtered_count)

    # 标记 is_target 并组装 CSV 行
    csv_rows = []
    target_count = 0
    for r in rows:
        is_target = 1 if (target_sid is not None and r["sender_id"] == target_sid) else 0
        if is_target:
            target_count += 1
        csv_rows.append({
            "created_at": ms_to_cst(r.get("created_at")) or "",
            "sender_name": r.get("sender_name") or "",
            "text": r.get("text") or "",
            "is_target": is_target,
        })

    # 查群名
    group_name = ""
    for g in get_group_list():
        if g["gid"] == gid:
            group_name = g.get("name", "")
            break

    # 确定输出路径
    if not output_path:
        sender_slug = target_sname or (str(target_sid) if target_sid else "member")
        safe = "".join(c for c in sender_slug if c.isalnum() or c in "._-") or "member"
        # 文件名中日期统一用 YYYY-MM-DD
        date_slug = date_str.replace("/", "-")
        _EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = str(
            _EXPORT_DIR / f"daily_{gid}_{safe}_{date_slug}.csv"
        )

    # 写 CSV（utf-8-sig，Excel 友好）
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["created_at", "sender_name", "text", "is_target"]
        )
        writer.writeheader()
        for row in csv_rows:
            writer.writerow(row)

    context_count = len(csv_rows) - target_count
    if filtered_count:
        log.info(
            "已导出日报 %d 条消息（目标 %d / 上下文 %d，过滤噪声 %d 条）→ %s",
            len(csv_rows), target_count, context_count,
            filtered_count, output_path,
        )
    else:
        log.info(
            "已导出日报 %d 条消息（目标 %d / 上下文 %d）→ %s",
            len(csv_rows), target_count, context_count, output_path,
        )

    return {
        "path": output_path,
        "count": len(csv_rows),
        "target_count": target_count,
        "context_count": context_count,
        "date": date_str,
        "group_name": group_name,
        "target_sender_id": target_sid,
        "target_sender_name": target_sname,
    }
