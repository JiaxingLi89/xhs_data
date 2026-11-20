import csv
import json
import re
import zipfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo
from xml.sax.saxutils import escape
from .promotion import EXPORT_COLUMNS, annotation_fields


NOTE_COLUMNS = ["note_id", "state", "title", "text", "type", "published_at", "tags", "liked_count",
                "collected_count", "comment_count", "share_count", "found_at", "fetched_at", "finished_at", "stop_reason"]
NOTE_COLUMNS += ['author_user_id', 'author_ip_location', 'identity_observed_at']
NOTE_COLUMNS += EXPORT_COLUMNS
COMMENT_COLUMNS = ["comment_id", "note_id", "content", "published_at", "like_count", "fetched_at",
                   'commenter_user_id', 'commenter_ip_location', 'identity_observed_at']
SOURCE_COLUMNS = ["note_id", "keyword", "sort", "page", "rank", "discovered_at", "search_id", "sampling_version"]


def csv_value(value):
    value = "" if value is None else str(value)
    # Prevent spreadsheet formulas when opening text exported from third parties.
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r")) else value


def numeric_likes(value):
    text = str(value or "").strip().replace(",", "").rstrip("+").lower()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([万亿wk]?)", text)
    if not match:
        return None
    multipliers = {"": 1, "k": 1000, "w": 10000, "万": 10000, "亿": 100000000}
    return Decimal(match[1]) * multipliers[match[2]]


def normalized_time(value, timezone_name):
    text = str(value if value is not None else "").strip()
    try:
        number = Decimal(text)
    except InvalidOperation:
        number = None
    if number is not None:
        if not number.is_finite() or number < 0:
            return None
        # Platform timestamps use Unix seconds or milliseconds.
        return number / 1000 if number >= 100000000000 else number
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
        return Decimal(str(parsed.timestamp()))
    except (ValueError, OverflowError, OSError):
        return None


def comment_order(row, order, timezone_name):
    value = (numeric_likes(row["like_count"]) if order == "likes_desc"
             else normalized_time(row["published_at"], timezone_name))
    key = 0 if value is None else (-value if order != "time_asc" else value)
    return row["note_id"], value is None, key, row["comment_id"]


def sheets(store, comment_sort="likes_desc", timezone_name="Asia/Shanghai"):
    if comment_sort not in {"likes_desc", "time_asc", "time_desc"}:
        raise ValueError("comment_sort")
    notes = []
    for row in store.db.execute("SELECT * FROM notes WHERE detail IS NOT NULL ORDER BY found_at,note_id"):
        data = json.loads(row["detail"])
        annotation = store.db.execute('SELECT * FROM promotion_annotations WHERE note_id=?', (row['note_id'],)).fetchone()
        data.update(annotation_fields(annotation, data))
        data.update({"note_id": row["note_id"], "state": row["state"], "found_at": row["found_at"],
                     "fetched_at": row["fetched_at"], "finished_at": row["finished_at"], "stop_reason": row["reason"]})
        data["tags"] = json.dumps(data.get("tags", []), ensure_ascii=False)
        notes.append([data.get(k, "") for k in NOTE_COLUMNS])
    comment_rows = sorted(store.db.execute("SELECT * FROM comments"),
                          key=lambda row: comment_order(row, comment_sort, timezone_name))
    comments = [[row[k] for k in COMMENT_COLUMNS] for row in comment_rows]
    sources = [[row[k] for k in SOURCE_COLUMNS] for row in store.db.execute("SELECT * FROM sources ORDER BY discovered_at,note_id")]
    return {"notes": [NOTE_COLUMNS] + notes, "comments": [COMMENT_COLUMNS] + comments, "sources": [SOURCE_COLUMNS] + sources}


def filtered_tables(tables):
    note_header = tables['notes'][0]
    id_index = note_header.index('note_id')
    flag_index = note_header.index('exclude_confirmed_promotion')
    included = {row[id_index] for row in tables['notes'][1:] if not row[flag_index]}
    return {name: [rows[0]] + [row for row in rows[1:] if row[rows[0].index('note_id')] in included]
            for name, rows in tables.items()}


def write_tables(directory, tables):
    directory.mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        with (directory / (name + '.csv')).open('w', encoding='utf-8-sig', newline='') as stream:
            csv.writer(stream).writerows([[csv_value(cell) for cell in row] for row in rows])
    write_xlsx(directory / 'research.xlsx', tables)


def column_name(index):
    result = ""
    while index:
        index, digit = divmod(index - 1, 26)
        result = chr(65 + digit) + result
    return result


def write_xlsx(path, tables):
    """Small standards-based XLSX writer; all cells are inline strings, never formulas."""
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package_rel = "http://schemas.openxmlformats.org/package/2006/relationships"
    declaration = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        types = '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        workbook = '<workbook xmlns="%s" xmlns:r="%s"><sheets>' % (ns, rel)
        relationships = '<Relationships xmlns="%s">' % package_rel
        for number, (name, rows) in enumerate(tables.items(), 1):
            filename = "worksheets/sheet%d.xml" % number
            types += '<Override PartName="/xl/%s" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % filename
            workbook += '<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (name, number, number)
            relationships += '<Relationship Id="rId%d" Type="%s/worksheet" Target="%s"/>' % (number, rel, filename)
            body = ['<worksheet xmlns="%s"><sheetData>' % ns]
            for row_number, row in enumerate(rows, 1):
                body.append('<row r="%d">' % row_number)
                for col, value in enumerate(row, 1):
                    value = "" if value is None else str(value)
                    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
                    # CSV keeps the full text; Excel has a 32767-character cell limit.
                    value = value[:32767]
                    body.append('<c r="%s%d" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (column_name(col), row_number, escape(value)))
                body.append("</row>")
            body.append("</sheetData></worksheet>")
            archive.writestr("xl/" + filename, declaration + "".join(body))
        archive.writestr("[Content_Types].xml", declaration + types + "</Types>")
        archive.writestr("xl/workbook.xml", declaration + workbook + "</sheets></workbook>")
        archive.writestr("xl/_rels/workbook.xml.rels", declaration + relationships + "</Relationships>")
        archive.writestr("_rels/.rels", declaration + '<Relationships xmlns="%s"><Relationship Id="rId1" Type="%s/officeDocument" Target="xl/workbook.xml"/></Relationships>' % (package_rel, rel))


def export_data(store, config, now, demo=False):
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = config.output_dir / stamp
    directory.mkdir(parents=True, mode=0o700)
    with store.transaction():
        tables = sheets(store, config.comment_sort, config.timezone)
        filtered = filtered_tables(tables)
        identity_status = store.identity_status()
        identity_jobs = [dict(row) for row in store.db.execute(
            'SELECT note_id,state,phase,pages,reason,created_at,finished_at FROM identity_jobs ORDER BY created_at,note_id')]
        comment_status = [dict(row) for row in store.db.execute(
            "SELECT note_id,state,reason AS stop_reason,"
            "CASE WHEN state='complete' AND reason='exhausted' THEN 1 ELSE 0 END AS interface_exhausted "
            "FROM notes WHERE detail IS NOT NULL ORDER BY note_id")]
    write_tables(directory, tables)
    write_tables(directory / 'without_confirmed_promotion', filtered)
    note_header = tables['notes'][0]
    reviewed = sum(row[note_header.index('human_review_current')] for row in tables['notes'][1:])
    versions = sorted({row[note_header.index('classification_version')] for row in tables['notes'][1:]
                       if row[note_header.index('classification_version')]})
    manifest = {"created_at": now, "synthetic_demo": demo, "stage": config.stage,
                "keywords": config.keywords, "sort": config.sort, "sampling_version": config.sampling_version,
                "comments_per_note": config.comments_per_note, "max_comment_pages": config.max_comment_pages,
                "comment_sort": config.comment_sort, "comment_status": comment_status,
                "identity_backfill": identity_status,
                "schema_version": store.get('schema_version'),
                "identity_jobs": identity_jobs,
                "promotion_filter": {'criterion': '仅排除当前正文经人工确认为明确商业推广的笔记',
                    'filtered_directory': 'without_confirmed_promotion',
                    'filtered_counts': {k: len(v)-1 for k, v in filtered.items()},
                    'excluded_note_count': len(tables['notes'])-len(filtered['notes']),
                    'reviewed_note_count': reviewed,
                    'review_coverage': reviewed / (len(tables['notes'])-1) if len(tables['notes']) > 1 else 0,
                    'classification_versions': versions},
                "counts": {k: len(v) - 1 for k, v in tables.items()},
                "notes": ["平台搜索和评论返回顺序下的方便样本，不是随机样本或全量数据。",
                          "时间戳保留平台原值，采集时间为Unix秒；互动数保留原字符串。",
                          "一级评论按接口游标采集，导出按帖子分组并按所选规则排序；缺失值置后，同值按评论ID排序。",
                          "interface_exhausted仅表示接口当前返回结束，不保证覆盖全部历史评论；不采集二级回复。",
                          "导出包含原始平台用户ID及接口返回的IP属地；不含昵称、头像和会话凭证。",
                          "IP属地不是常住地；identity_observed_at为字段获取时间，补采值不代表最初采集时的属地。",
                          "缺失字段留空；补采接口结束不代表字段全部补齐；分析时结合笔记状态与补采状态。",
                          "推广初筛只标记文字线索，不证明广告真实性；未发现线索不等于真实分享。",
                          "筛选版仅排除人工确认推广且复核对应当前正文的帖子；全量原始数据保留，完成状态仍需单独筛选。",
                          "CSV保留完整文本并转义公式前缀；XLSX单元格最多32767字符。"]}
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return directory
