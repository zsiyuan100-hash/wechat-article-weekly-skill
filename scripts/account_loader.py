#!/usr/bin/env python3
"""Load WeChat account names and categories from XLSX, JSON, or TXT.

The loader intentionally uses only the Python standard library so the shared
repository can run without a package-install step.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _tag(name: str) -> str:
    return f"{{{MAIN_NS}}}{name}"


def _column_index(cell_ref: str) -> int:
    letters = re.match(r"[A-Za-z]+", cell_ref or "")
    if not letters:
        return 0
    value = 0
    for char in letters.group(0).upper():
        value = value * 26 + ord(char) - ord("A") + 1
    return value - 1


def _shared_strings(archive: ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return ["".join(node.itertext()) for node in root.findall(_tag("si"))]


def _first_sheet_path(archive: ZipFile) -> str:
    """Resolve the first workbook sheet, with a simple sheet1 fallback."""
    try:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        sheet = workbook.find(f"{_tag('sheets')}/{_tag('sheet')}")
        rel_id = sheet.get(f"{{{REL_NS}}}id") if sheet is not None else None
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        if rel_id:
            for relation in rels.findall(f"{{{PKG_REL_NS}}}Relationship"):
                if relation.get("Id") == rel_id:
                    target = relation.get("Target", "")
                    if target.startswith("/"):
                        return target.lstrip("/")
                    return "xl/" + target.lstrip("/")
    except (KeyError, ET.ParseError):
        pass
    return "xl/worksheets/sheet1.xml"


def _load_xlsx(path: Path) -> list[dict[str, str]]:
    with ZipFile(path) as archive:
        strings = _shared_strings(archive)
        root = ET.fromstring(archive.read(_first_sheet_path(archive)))
        rows: list[list[str]] = []
        for row in root.findall(f".//{_tag('row')}"):
            cells: dict[int, str] = {}
            for cell in row.findall(_tag("c")):
                index = _column_index(cell.get("r", ""))
                cell_type = cell.get("t", "")
                if cell_type == "inlineStr":
                    value = "".join(cell.itertext())
                else:
                    value_node = cell.find(_tag("v"))
                    value = value_node.text if value_node is not None else ""
                    if cell_type == "s" and value:
                        value = strings[int(value)]
                cells[index] = value.strip()
            if cells:
                width = max(cells) + 1
                rows.append([cells.get(index, "") for index in range(width)])

    if not rows:
        return []

    header = [value.casefold().strip() for value in rows[0]]
    name_headers = {"名称", "公众号", "账号", "name", "account", "account_name"}
    category_headers = {"类型", "分类", "类别", "category"}
    name_index = next((i for i, value in enumerate(header) if value in name_headers), 0)
    category_index = next(
        (i for i, value in enumerate(header) if value in category_headers),
        1,
    )

    accounts: list[dict[str, str]] = []
    for row in rows[1:]:
        name = row[name_index].strip() if name_index < len(row) else ""
        category = row[category_index].strip() if category_index < len(row) else ""
        if name and not name.startswith("http"):
            accounts.append({"name": name, "category": category})
    return accounts


def _load_json(path: Path) -> list[dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("accounts", [])
    if not isinstance(data, list):
        raise ValueError(f"Expected a list of accounts in {path}")
    accounts = []
    for item in data:
        if isinstance(item, str):
            name, category = item.strip(), ""
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("公众号") or "").strip()
            category = str(item.get("category") or item.get("分类") or "").strip()
        else:
            continue
        if name and not name.startswith("http"):
            accounts.append({"name": name, "category": category})
    return accounts


def _load_text(path: Path) -> list[dict[str, str]]:
    return [
        {"name": line.strip(), "category": ""}
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#") and not line.strip().startswith("http")
    ]


def load_accounts(path: str | Path) -> list[dict[str, str]]:
    """Load account records from a supported file path."""
    file_path = Path(path)
    suffix = file_path.suffix.casefold()
    if suffix == ".xlsx":
        return _load_xlsx(file_path)
    if suffix == ".json":
        return _load_json(file_path)
    if suffix in {".txt", ".list"}:
        return _load_text(file_path)
    raise ValueError(f"Unsupported account file type: {file_path}")


def account_names(accounts: list[dict[str, str]]) -> list[str]:
    return [item["name"] for item in accounts if item.get("name")]


def category_map(accounts: list[dict[str, str]]) -> dict[str, str]:
    return {item["name"]: item.get("category", "") for item in accounts if item.get("name")}
