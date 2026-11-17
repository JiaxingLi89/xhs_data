import csv
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape


NOTE_COLUMNS = ["note_id", "state", "title", "text", "type", "published_at", "tags", "liked_count",
                "collected_count", "comment_count", "share_count", "found_at", "fetched_at", "finished_at", "stop_reason"]
COMMENT_COLUMNS = ["comment_id", "note_id", "content", "published_at", "like_count", "fetched_at"]
SOURCE_COLUMNS = ["note_id", "keyword", "sort", "page", "rank", "discovered_at", "search_id", "sampling_version"]


def csv_value(value):
    value = "" if value is None else str(value)
    # Prevent spreadsheet formulas when opening text exported from third parties.
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r")) else value


def sheets(store):
    notes = []
    for row in store.db.execute("SELECT * FROM notes WHERE detail IS NOT NULL ORDER BY found_at,note_id"):
        data = json.loads(row["detail"])
        data.update({"note_id": row["note_id"], "state": row["state"], "found_at": row["found_at"],
                     "fetched_at": row["fetched_at"], "finished_at": row["finished_at"], "stop_reason": row["reason"]})
        data["tags"] = json.dumps(data.get("tags", []), ensure_ascii=False)
        notes.append([data.get(k, "") for k in NOTE_COLUMNS])
    comments = [[row[k] for k in COMMENT_COLUMNS] for row in store.db.execute("SELECT * FROM comments ORDER BY note_id,fetched_at,comment_id")]
    sources = [[row[k] for k in SOURCE_COLUMNS] for row in store.db.execute("SELECT * FROM sources ORDER BY discovered_at,note_id")]
    return {"notes": [NOTE_COLUMNS] + notes, "comments": [COMMENT_COLUMNS] + comments, "sources": [SOURCE_COLUMNS] + sources}


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
        tables = sheets(store)
    for name, rows in tables.items():
        with (directory / (name + ".csv")).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerows([[csv_value(cell) for cell in row] for row in rows])
    write_xlsx(directory / "research.xlsx", tables)
    manifest = {"created_at": now, "synthetic_demo": demo, "stage": config.stage,
                "keywords": config.keywords, "sort": config.sort, "sampling_version": config.sampling_version,
                "comments_per_note": config.comments_per_note, "counts": {k: len(v) - 1 for k, v in tables.items()},
                "notes": ["平台搜索和评论返回顺序下的方便样本，不是随机样本或全量数据。",
                          "时间戳保留平台原值，采集时间为Unix秒；互动数保留原字符串。",
                          "导出包含状态标记，分析时应筛选完整样本；未导出会话凭证和用户资料。",
                          "CSV保留完整文本并转义公式前缀；XLSX单元格最多32767字符。"]}
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return directory

