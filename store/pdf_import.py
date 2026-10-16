"""
Reads a quotation PDF and returns the customer details and the list of items, for the "New customer" page.
It is a best guess: the page shows everything for checking and editing before the customer is created.
Works with PDFs that have real text (made by software, not a scan or a photo).  Pure Python: no Django here.
"""
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

NAME_HEADERS = ("particular", "particulars", "description", "item", "items", "material", "product", "work")
SPEC_HEADERS = ("specification", "specifications", "spec", "details", "detail")
QTY_HEADERS = ("qty", "quantity", "nos", "no's", "units")
MAKE_HEADERS = ("make", "brand", "company", "manufacturer")
RATE_HEADERS = ("rate", "unit rate", "unit price", "price", "unit cost")
SERIAL_HEADERS = ("#", "sr no", "srno", "sl no", "slno", "s no", "sno", "sr", "sl", "no")
UNITS = ("nos", "no", "pcs", "pc", "set", "sets", "lot", "m", "mtr", "mtrs", "meter", "meters", "kg", "pack", "packs", "roll", "rolls", "kw", "w")


class PdfReadError(Exception):
    pass


def _clean(cell):
    return " ".join(str(cell or "").split())


def _header_kind(text):
    text = re.sub(r"[^a-z'# ]", "", text.lower()).strip()
    for kind, words in (("serial", SERIAL_HEADERS), ("name", NAME_HEADERS), ("spec", SPEC_HEADERS), ("qty", QTY_HEADERS), ("make", MAKE_HEADERS), ("rate", RATE_HEADERS)):
        if text in words or any(text.startswith(w + " ") for w in words):
            return kind
    return None


def _column_map(row):
    kinds = {}
    for index, cell in enumerate(row):
        kind = _header_kind(_clean(cell))
        if kind and kind not in kinds:
            kinds[kind] = index
    return kinds if "qty" in kinds and ("name" in kinds or "spec" in kinds) else None


def parse_quantity(text):
    """'9 Nos' -> (Decimal('9'), 'Nos');  '50 m' -> (50, 'm');  '1' -> (1, 'Nos');  'Lot' -> (1, 'Lot')."""
    text = _clean(text).replace(",", "")
    match = re.match(r"^(\d+(?:\.\d+)?)\s*([A-Za-z']*)", text)
    if match:
        unit = match.group(2)
        return Decimal(match.group(1)), (unit if unit.lower() in UNITS else "Nos") if unit else "Nos"
    word = text.lower()
    if word in UNITS:
        return Decimal("1"), text.title()
    return None, "Nos"


def parse_money(text):
    text = re.sub(r"[^\d.]", "", _clean(text).replace(",", ""))
    try:
        return Decimal(text) if text else Decimal("0")
    except InvalidOperation:
        return Decimal("0")


def _rows_to_items(rows, kinds):
    items = []
    for row in rows:
        cells = [_clean(c) for c in row]
        if not any(cells):
            continue
        get = lambda kind: cells[kinds[kind]] if kind in kinds and kinds[kind] < len(cells) else ""
        quantity, unit = parse_quantity(get("qty"))
        name, spec = get("name"), get("spec")
        if quantity is None or not (name or spec):
            continue                                       # a total / heading / note row
        if re.match(r"^(grand\s*)?total|^sub\s*total", name.lower()):
            continue
        full = " ".join(x for x in (name, spec) if x)
        items.append({"name": full[:250], "make": get("make")[:100], "unit": unit, "quantity": quantity, "rate": parse_money(get("rate"))})
    return items


def _text_in(words, left, right, top, bottom):
    """The words whose middle is inside this box, in reading order, as one text."""
    hit = [w for w in words if left <= (w["x0"] + w["x1"]) / 2 < right and top - 1 <= (w["top"] + w["bottom"]) / 2 <= bottom + 1]
    hit.sort(key=lambda w: (round(w["top"] / 3), w["x0"]))
    return _clean(" ".join(w["text"] for w in hit))


def _row_items(page, table, state):
    """
    Rows of one table.  The column edges come from the header row (kept for the next pages, which have no header), and the words of every
    row are put into the columns by their position, so rows the PDF drew as one merged cell are read correctly too.
    """
    words = page.extract_words()
    items = []
    for row in table.rows:
        boxes = [c for c in row.cells if c]
        if not boxes:
            continue
        top, bottom = min(c[1] for c in boxes), max(c[3] for c in boxes)
        # the header row: its cells are separate and say Qty / Particular ...
        header_texts = [_text_in(words, c[0], c[2], c[1], c[3]) if c else "" for c in row.cells]
        found = _column_map(header_texts)
        if found:
            serial = next((i for i, h in enumerate(header_texts) if _header_kind(h) == "serial"), None)
            if serial is not None:
                found["serial"] = serial
            edges = [(c[0], c[2]) if c else None for c in row.cells]
            state["kinds"], state["edges"] = found, edges
            continue
        kinds, edges = state.get("kinds"), state.get("edges")
        if not kinds or not edges:
            continue
        # the text of every column of this row, by position
        cells = []
        for edge in edges:
            cells.append(_text_in(words, edge[0], edge[1], top, bottom) if edge else "")
        get = lambda kind: cells[kinds[kind]] if kind in kinds and kinds[kind] < len(cells) else ""
        name, spec = get("name"), get("spec")
        serial_text = get("serial")
        if not (name or spec):
            continue
        if re.match(r"^(grand\s*)?total|^sub\s*total|^system summary", name.lower()):
            continue
        if "serial" in kinds and not re.match(r"^\d+\.?$", serial_text or ""):
            continue                                       # a heading / note row, not a numbered item
        quantity, unit = parse_quantity(get("qty"))
        if quantity is None:
            quantity, unit = Decimal("1"), "Nos"           # numbered item without a quantity (for example "Lugs, Gland"): 1, to be corrected
        full = " ".join(x for x in (name, spec) if x)
        if unit == "Nos" and quantity >= 5 and re.search(r"cable|wire", full, re.I):
            unit = "m"
        make = get("make")
        items.append({"name": full[:250], "make": ("" if make.strip() in ("-", "--") else make)[:100], "unit": unit, "quantity": quantity,
                      "rate": parse_money(get("rate"))})
    return items


def find_items(pdf):
    """The items of the table(s) that have a Qty column, over all pages (a table that goes on to the next page without a header is joined)."""
    items, state, warnings = [], {}, []
    for page in pdf.pages:
        for table in page.find_tables():
            items.extend(_row_items(page, table, state))
    if not items:
        warnings.append("No table of materials (with a Qty column) was found in this PDF.")
    return items, warnings


def find_fields(text):
    fields = {"name": "", "person": "", "offer_no": "", "phone": "", "location": "", "system_size": ""}
    joined = re.sub(r"(?<=\d)[ -](?=\d)", "", text)                  # 98470 12345 / 98470-12345 -> 9847012345
    phone = (re.search(r"(?:phone|mobile|mob|contact|tel)[^\d\n]{0,15}(?:\+?91)?(\d{10})(?!\d)", joined, re.I)
             or re.search(r"(?<!\d)(?:\+?91)?([6-9]\d{9})(?!\d)", joined))
    if phone:
        fields["phone"] = phone.group(1)
    size = re.search(r"(\d+(?:\.\d+)?)\s*kw(?:p)?\b", text, re.I)
    if size:
        fields["system_size"] = size.group(1)
    offer = re.search(r"(?:quotation|quote|offer|ref(?:erence)?)\s*(?:no|number|#)?\.?\s*[:\-]\s*([A-Za-z0-9][A-Za-z0-9/\-_.]{2,})", text, re.I) \
        or re.search(r"offer\s*no\.?\s*:?\s*([A-Za-z]{2,}/[A-Za-z0-9/\- ]{2,40}?)(?=\s{2,}|\n|\s(?:category|dated|capacity))", text, re.I)
    if offer:
        fields["offer_no"] = offer.group(1)
    business = re.search(r"business\s*name\s*[:\-]\s*([^\n]{2,80})", text, re.I)
    if business:
        fields["name"] = _clean(business.group(1))
    name = None if fields["name"] else re.search(r"(?:customer\s*name|client\s*name|name\s*of\s*(?:the\s*)?(?:customer|client)|customer|client|to)\s*[:\-]\s*([^\n]{2,80})", text, re.I)
    if name:
        fields["name"] = _clean(name.group(1))
    place = re.search(r"(?:location|place|site|address)\s*[:\-]\s*([^\n]{2,80})", text, re.I)
    if place:
        fields["location"] = _clean(place.group(1))
    return fields


SERVICE_WORDS = ("documentation", "design", "amc", "engineering", "transportation", "loading")


def grid_value(words, label, next_label=None):
    """For a header grid ("OFFER NO  CATEGORY  LOCATION" with the values on the line below): the text under the label."""
    heads = [w for w in words if w["text"].upper().strip(":") == label]
    if not heads:
        return ""
    head = heads[0]
    right = next((w["x0"] for w in words if next_label and w["text"].upper().strip(":") == next_label and w["x0"] > head["x0"] and abs(w["top"] - head["top"]) < 4), head["x0"] + 170)
    below = [w for w in words if head["bottom"] <= w["top"] <= head["bottom"] + 40 and head["x0"] - 5 <= w["x0"] < right - 2]
    if not below:
        return ""
    first = min(w["top"] for w in below)
    line = sorted((w for w in below if abs(w["top"] - first) < 4), key=lambda w: w["x0"])
    return "".join(w["text"] for w in line) if label == "OFFER" else _clean(" ".join(w["text"] for w in line))


def read_pdf(file_like):
    """-> {"fields": {...}, "items": [...], "warnings": [...]}.  Raises PdfReadError when the file cannot be read."""
    try:
        import pdfplumber
    except ImportError:
        raise PdfReadError("The PDF reader is not installed (pip install pdfplumber).")
    try:
        with pdfplumber.open(file_like) as pdf:
            if not pdf.pages:
                raise PdfReadError("That PDF has no pages.")
            all_text = "\n".join((page.extract_text() or "") for page in pdf.pages)
            text = "\n".join((page.extract_text() or "") for page in pdf.pages[:3])
            if len(text.strip()) < 20:
                raise PdfReadError("This PDF has no readable text (it looks like a scan or a photo). Type the customer in instead.")
            items, warnings = find_items(pdf)
            fields = find_fields(text)
            if not fields["location"] or not fields["offer_no"]:
                first_words = pdf.pages[0].extract_words()
                fields["location"] = fields["location"] or grid_value(first_words, "LOCATION", None)
                fields["offer_no"] = fields["offer_no"] or grid_value(first_words, "OFFER", "CATEGORY")
    except PdfReadError:
        raise
    except Exception as problem:
        raise PdfReadError("Could not read that PDF (%s)." % problem.__class__.__name__)
    for item in items:
        item["service"] = bool(re.match(r"^(%s)\b" % "|".join(SERVICE_WORDS), item["name"].lower()))
    return {"fields": fields, "items": items, "warnings": warnings, "text": all_text}


# ---------------------------------------------------------------------------------------------- only the specifications
def _plain(number):
    """Decimal('50') -> '50' (never 5E+1), Decimal('1.50') -> '1.5'."""
    return format(Decimal(number).normalize(), "f")


def _first_brand(make):
    return re.split(r"[/,&]| or ", make or "", maxsplit=1)[0].strip()


def specs_from_items(items, fields):
    """
    The few things the quotation website needs to work a quotation out again: system size and phase, the panel (brand, watts, technology, how
    many), the inverter kW, the structure, and the DC / AC cable lengths.  Read from the items of the PDF; every value is shown for checking.
    """
    specs = {"system_size": fields.get("system_size", ""), "phase": "", "panel_company": "", "panel_type": "", "panel_watts": "",
             "panel_watts_max": "", "panel_quantity": "", "dcr": "", "inverter_kw": "", "inverter_brand": "", "structure": "roof",
             "dc_cable_m": "", "dc_cable_sqmm": "", "ac_wire_m": ""}
    for item in items:
        text = "%s %s" % (item["name"], item["make"])
        low = text.lower()
        head = item["name"].lower()
        if not specs["panel_company"] and re.match(r"^(pv\s*module|solar\s*(pv\s*)?(module|panel)|module|panel)", head):
            specs["panel_company"] = _first_brand(item["make"])
            specs["panel_quantity"] = _plain(item["quantity"]) if item["quantity"] else ""
            rng = re.search(r"(\d{3})\s*(?:-|to|\u2013)\s*(\d{3})\s*(?:wp|w)\b", low)
            single = re.search(r"(\d{3})\s*(?:wp|w)\b", low)
            if rng:
                specs["panel_watts"], specs["panel_watts_max"] = rng.group(1), rng.group(2)
            elif single:
                specs["panel_watts"] = single.group(1)
            for word in ("topcon", "bifacial", "perc", "mono"):
                if word in low:
                    specs["panel_type"] = word.upper() if word == "perc" else word.title() if word != "topcon" else "TOPCon"
                    break
            if "non-dcr" in low or "non dcr" in low:
                specs["dcr"] = "non_dcr"
            elif "dcr" in low:
                specs["dcr"] = "dcr"
        elif not specs["inverter_kw"] and "inverter" in head:
            kw = re.search(r"(\d+(?:\.\d+)?)\s*kw", low)
            specs["inverter_kw"] = kw.group(1) if kw else ""
            specs["inverter_brand"] = _first_brand(item["make"])
            if re.search(r"3\s*ph|three[\s-]*phase", low):
                specs["phase"] = "three"
            elif re.search(r"1\s*ph|single[\s-]*phase", low):
                specs["phase"] = "single"
        elif re.search(r"mounting|structure", head) and "walkway" not in head:
            specs["structure"] = "roof" if "roof" in low else ("gi" if re.search(r"\bgi\b", low) else "gp" if re.search(r"\bgp\b", low) else "roof")
        elif head.startswith("dc cable") and not specs["dc_cable_m"]:
            specs["dc_cable_m"] = _plain(item["quantity"])
            sq = re.search(r"(\d+(?:\.\d+)?)\s*sq", low)
            specs["dc_cable_sqmm"] = sq.group(1) if sq else ""
        elif head.startswith("ac") and "cable" in head and not specs["ac_wire_m"] and not re.search(r"\bug\b|aluminium|aluminum|metering", low.split("sq")[0]) \
                and re.search(r"inverter\s*to\s*acdb|frls|flexible copper", low):
            specs["ac_wire_m"] = _plain(item["quantity"])
    if not specs["phase"]:
        specs["phase"] = "single"
    # The SYSTEM size is the size of the panels (3 kW of panels can go with a 5 kW inverter: a "3/5" quotation), so it comes from
    # the panels when they are known; the kW printed in the header of the quotation is often the inverter's.
    if specs["panel_watts"] and specs["panel_quantity"]:
        top = Decimal(specs["panel_watts_max"] or specs["panel_watts"])
        panel_kw = top * Decimal(specs["panel_quantity"]) / 1000
        specs["panel_kw"] = _plain(panel_kw.quantize(Decimal("0.01")))
        specs["system_size"] = _plain(max(panel_kw.quantize(Decimal("1"), rounding=ROUND_HALF_UP), Decimal("1")))
    return specs


def read_specs(file_like):
    """-> {"fields": customer details, "specs": the specifications, "warnings": [...]}"""
    result = read_pdf(file_like)
    specs = specs_from_items(result["items"], result["fields"])
    low = result["text"].lower()
    if not specs["dcr"] and re.search(r"non[\s-]*dcr", low):
        specs["dcr"] = "non_dcr"
    elif not specs["dcr"] and re.search(r"\bdcr\b", low):
        specs["dcr"] = "dcr"
    notes = list(result["warnings"])
    header_kw = result["fields"].get("system_size", "")
    if header_kw and specs["system_size"] and Decimal(header_kw) != Decimal(specs["system_size"]):
        notes.append("The PDF says %s kW, but its panels add up to %s kW, so the system size is taken as %s kW and the inverter as %s kW (a %s/%s quotation). Change them if that is wrong." % (
            header_kw, specs.get("panel_kw", specs["system_size"]), specs["system_size"], specs["inverter_kw"] or header_kw, specs["system_size"], specs["inverter_kw"] or header_kw))
    return {"fields": result["fields"], "specs": specs, "warnings": notes}
