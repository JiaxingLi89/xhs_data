"""Read externally maintained three-sheet workbooks for collection deduplication."""
from pathlib import Path
from xml.etree import ElementTree as ET
import re
import zipfile

from .errors import SetupError
from .export import NOTE_COLUMNS, COMMENT_COLUMNS, SOURCE_COLUMNS

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
HEADERS = {"notes": NOTE_COLUMNS, "comments": COMMENT_COLUMNS, "sources": SOURCE_COLUMNS}


def _column_index(reference):
    letters = re.match(r"[A-Z]+", reference)
    if not letters:
        raise ValueError("invalid cell reference")
    index = 0
    for letter in letters.group():
        index = index * 26 + ord(letter) - 64
    return index - 1


def _sheet_rows(archive, target, shared):
    root = ET.fromstring(archive.read(target))
    result = []
    for row in root.findall(f".//{{{NS}}}sheetData/{{{NS}}}row"):
        cells = {}
        for cell in row.findall(f"{{{NS}}}c"):
            kind = cell.get("t")
            if kind == "inlineStr":
                value = "".join(cell.itertext())
            elif kind == "s":
                value = shared[int(cell.findtext(f"{{{NS}}}v"))]
            else:
                value = cell.findtext(f"{{{NS}}}v") or ""
            cells[_column_index(cell.attrib["r"])] = value
        if cells:
            result.append([cells.get(i, "") for i in range(max(cells) + 1)])
    return result


def read_research(path):
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as archive:
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            targets = {node.attrib["Id"]: node.attrib["Target"] for node in relationships.findall(f"{{{PKG_REL}}}Relationship")}
            shared = []
            if "xl/sharedStrings.xml" in archive.namelist():
                shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                shared = ["".join(node.itertext()) for node in shared_root.findall(f"{{{NS}}}si")]
            tables = {}
            for sheet in workbook.findall(f".//{{{NS}}}sheet"):
                name = sheet.attrib["name"]
                if name not in HEADERS:
                    continue
                target = targets[sheet.attrib[f"{{{REL}}}id"]]
                target = target.lstrip("/") if target.startswith("/") else "xl/" + target
                rows = _sheet_rows(archive, target, shared)
                if not rows or rows[0] != HEADERS[name]:
                    raise ValueError("invalid header: " + name)
                tables[name] = rows
            if set(tables) != set(HEADERS):
                raise ValueError("missing required sheets")
            return tables
    except (OSError, zipfile.BadZipFile, KeyError, ValueError, ET.ParseError, IndexError) as exc:
        raise SetupError("无法读取总表：%s（%s）。请检查手动放入的 totalresearch.xlsx 文件及配置路径后重新启动。" % (path, exc)) from exc


def total_note_ids(path):
    tables = read_research(path)
    return {row[0] for name in ("notes", "sources") for row in tables[name][1:] if row and row[0]}


