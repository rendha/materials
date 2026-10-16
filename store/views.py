import csv
import json
import re
from pathlib import Path
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import F
from django.http import Http404, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from . import pdf_import, services
from .models import BosItem, Customer, Labour, Line, Stock

ZERO = Decimal("0")

# Items that are NOT part of the BOS list: the make changes from job to job, so the user types it or picks it from the warehouse.
# (name, text the warehouse item's name must contain, default quantity, default rate, default make)
VARIANT_ITEMS = [
    ("End cap", "end cap", Decimal("0"), Decimal("0"), ""),
    ("MC4 connector", "mc4", Decimal("4"), Decimal("40"), "ningbo"),
]


# ---------------------------------------------------------------------------------------------- who is who
def is_project(user):
    """The Project person: prepares the list and decides warehouse / purchase."""
    return user.is_superuser or user.groups.filter(name="Project").exists()


def is_store(user):
    """The Store person: keeps the warehouse stock and does the picking / purchasing."""
    return user.is_superuser or user.groups.filter(name="Store").exists()


def _roles(request):
    return is_project(request.user), is_store(request.user)


# ---------------------------------------------------------------------------------------------- warehouse stock
def release_stock(line):
    """Gives back to the warehouse whatever this line has taken out of it."""
    if line.applied_to_id and line.applied_qty:
        Stock.objects.filter(pk=line.applied_to_id).update(quantity=F("quantity") + line.applied_qty)
    line.applied_to = None
    line.applied_qty = ZERO


def sync_stock(line):
    """
    A 'take from warehouse' line that the store marked Done takes its quantity out of the linked stock item (never below 0).
    It first undoes its earlier effect, so saving twice or changing a line never takes the stock out twice.
    """
    release_stock(line)
    if line.source == Line.WAREHOUSE and line.store_status == Line.DONE and line.stock_item_id:
        stock = Stock.objects.get(pk=line.stock_item_id)
        take = min(line.quantity, stock.quantity) if stock.quantity > 0 else ZERO
        if take > 0:
            Stock.objects.filter(pk=stock.pk).update(quantity=F("quantity") - take)
            line.applied_to = stock
            line.applied_qty = take
    line.save()


def _norm(text):
    return " ".join(str(text or "").split()).casefold()


_UNIT_AFTER_NUMBER = re.compile(r"(\d+(?:\.\d+)?)\s*(kwp|kw|wp|w|sq\.?\s*mm|sqmm|mm|micron|kva|va|a|v|m)\b")


def _words(text):
    """
    'EARTH ROD 300 MICRON' -> {'earth', 'rod', '300micron'}.  Capitals, symbols and a trailing 's' do not matter, and a number
    sticks to its unit, so '580 W', '580W' and '580 wp' are the same, and so are '5 kW' and '5KW', '4 sq mm' and '4 SQMM'.
    """
    text = str(text or "").casefold().replace("single phase", "1ph").replace("single-phase", "1ph").replace("three phase", "3ph").replace("three-phase", "3ph").replace("poly cab", "polycab").replace("polcab", "polycab").replace("v guard", "vguard")
    text = _UNIT_AFTER_NUMBER.sub(lambda m: m.group(1) + {"wp": "w", "kwp": "kw", "sqmm": "sqmm", "va": "va"}.get(re.sub(r"[ .]", "", m.group(2)), re.sub(r"[ .]", "", m.group(2))), text)
    out = set()
    for word in re.findall(r"[a-z0-9]+", text):
        out.add(word[:-1] if len(word) > 3 and word.endswith("s") else word)
    return out


def find_stock(name, make):
    """
    The warehouse item for an item of a list.  The store person may type it shorter: every word of the warehouse item's name (and make)
    only has to be somewhere in the list item's name + make.  "Earth rod" matches "EARTH ROD 300 MICRON".
    When several match, the most specific one (the most words) wins; when two match equally the item is left for the person to pick.
    """
    wanted = _words("%s %s" % (name, make))
    best, best_score, tie = None, 0, False
    for stock in Stock.objects.all():
        own = _words(stock.name) | _words(stock.make)
        if not own or not own <= wanted:
            continue
        if len(own) > best_score:
            best, best_score, tie = stock, len(own), False
        elif len(own) == best_score:
            tie = True
    return None if tie else best


def relink(customer):
    """Links every item that is not linked yet to its warehouse item, so stock added after the customer was brought in is found."""
    if customer.status == Customer.DONE:
        return
    for line in customer.lines.filter(stock_item__isnull=True, pick_from=""):
        stock = find_stock(line.name, line.make)
        if stock:
            line.stock_item = stock
            line.save(update_fields=["stock_item"])


def reserved_by_stock():
    """{stock id: quantity already promised to open customers (warehouse lines the store has not taken yet)}."""
    reserved = {}
    open_lines = Line.objects.filter(source=Line.WAREHOUSE, stock_item__isnull=False).exclude(store_status=Line.DONE) \
        .exclude(customer__status=Customer.DONE)
    for line in open_lines:
        reserved[line.stock_item_id] = reserved.get(line.stock_item_id, ZERO) + line.quantity
    return reserved


def merge_balances(customer, group=None, force=False):
    """
    Puts a split item back together (the warehouse part + its 'balance' purchase line) so it can be decided again.
    force=False: only when the warehouse has MORE of it now than the part that was taken (new stock arrived).
    """
    lines = customer.lines.filter(store_note__startswith="balance", source=Line.PURCHASE).exclude(store_status=Line.DONE)
    if group:
        lines = lines.filter(group=group)
    reserved = reserved_by_stock()
    merged = 0
    for balance in list(lines):
        parent = customer.lines.filter(name=balance.name, make=balance.make, group=balance.group, source=Line.WAREHOUSE,
                                       stock_item__isnull=False).exclude(store_note__startswith="balance").exclude(store_status=Line.DONE).first()
        if parent is None:
            continue
        if not force:
            free = parent.stock_item.quantity - (reserved.get(parent.stock_item_id, ZERO) - parent.quantity)
            if free <= parent.quantity:
                continue
        parent.quantity += balance.quantity
        parent.source = Line.UNDECIDED
        parent.save()
        balance.delete()
        merged += 1
    return merged


def auto_decide(customer, group=None):
    """
    Decides every 'Not decided' item from the warehouse list (the project person can change any of it afterwards):
      - in the warehouse and enough free  -> take from warehouse
      - in the warehouse but only some    -> the free part from the warehouse, the rest as a new purchase line (BOS items too)
      - not in the warehouse / none left  -> purchase
    "Free" = the warehouse quantity minus what other open customers (and earlier items here) already hold.
    Returns how many items went to warehouse / purchase.
    """
    relink(customer)
    reserved = reserved_by_stock()
    result = {"warehouse": 0, "purchase": 0, "split": 0}
    with transaction.atomic():
        for line in list(customer.lines.filter(source=Line.UNDECIDED, quantity__gt=0, **({"group": group} if group else {})).select_related("stock_item")):
            stock = line.stock_item
            free = (stock.quantity - reserved.get(stock.pk, ZERO)) if stock else ZERO
            if line.pick_from and not stock and Stock.objects.filter(quantity__gt=0).exists() and any(
                    line.pick_from in ("%s %s" % (s.name, s.make)).lower() for s in Stock.objects.filter(quantity__gt=0)):
                continue                  # End cap / MC4 style: several types are in the warehouse, so the person picks the type
            if stock and free >= line.quantity:
                line.source = Line.WAREHOUSE
                reserved[stock.pk] = reserved.get(stock.pk, ZERO) + line.quantity
                result["warehouse"] += 1
            elif stock and free > 0:
                balance = line.quantity - free
                last = customer.lines.order_by("-position").first()
                Line.objects.create(customer=customer, group=line.group, position=last.position + 1, name=line.name, make=line.make,
                                    unit=line.unit, quantity=balance, rate=line.rate, source=Line.PURCHASE,
                                    store_note="balance: not enough in the warehouse")
                line.quantity = free
                line.source = Line.WAREHOUSE
                reserved[stock.pk] = reserved.get(stock.pk, ZERO) + free
                result["warehouse"] += 1
                result["purchase"] += 1
                result["split"] += 1
            else:
                line.source = Line.PURCHASE
                result["purchase"] += 1
            line.save()
    return result


def _needed(lines):
    """Only lines with a quantity need a decision / the store's work (a BOS item with quantity 0 is not needed)."""
    return [l for l in lines if l.quantity > 0]


# ---------------------------------------------------------------------------------------------- login / home
@login_required
def home(request):
    project, store = _roles(request)
    customers = Customer.objects.prefetch_related("lines")
    if not project:
        customers = customers.exclude(status=Customer.DRAFT)       # the store sees a customer only after it was sent
    rows = []
    for customer in customers:
        lines = _needed(list(customer.lines.all()))
        rows.append({
            "customer": customer, "total": len(lines),
            "warehouse": sum(1 for l in lines if l.source == Line.WAREHOUSE),
            "purchase": sum(1 for l in lines if l.source == Line.PURCHASE),
            "undecided": sum(1 for l in lines if l.source == Line.UNDECIDED),
            "done": sum(1 for l in lines if l.store_status == Line.DONE),
            "short": sum(1 for l in lines if l.store_status == Line.SHORT),
        })
    return render(request, "store/home.html", {"rows": rows, "project": project, "store": store})


# ---------------------------------------------------------------------------------------------- quotation -> customer
def create_customer(data):
    """Builds a customer (lines, BOS items, labour) from the quotation website's data (a dict with an 'items' list)."""
    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise services.QuotationApiError("That quotation has no material list.")

    panel_count = sum((services.to_decimal(i.get("quantity"), ZERO) for i in items if "solar panel" in str(i.get("name", "")).lower()), ZERO)
    bos_quoted, main_items = ZERO, []
    for item in items:
        if str(item.get("name", "")).startswith("BOS items"):      # the single BOS lot of the quotation: kept only as the quoted amount
            bos_quoted = services.to_decimal(item.get("rate"), ZERO) * services.to_decimal(item.get("quantity"), Decimal("1"))
        else:
            main_items.append(item)

    with transaction.atomic():
        customer = Customer.objects.create(
            quotation_id=data.get("id") if str(data.get("id", "")).isdigit() else None,
            offer_no=str(data.get("offer_no", ""))[:100], name=str(data.get("business_name") or "Customer")[:200],
            person=str(data.get("customer_name", ""))[:200], phone=str(data.get("phone", ""))[:50],
            location=str(data.get("location", ""))[:200], system_size=str(data.get("system_size", ""))[:20],
            bos_quoted=bos_quoted)
        position = 0

        def add(name, make, unit, quantity, rate, group=Line.MAIN, pick_from=""):
            nonlocal position
            name = str(name).strip()[:250]
            if not name:
                return
            Line.objects.create(
                customer=customer, position=position, name=name, make=str(make or "")[:100], unit=(str(unit or "Nos")[:20] or "Nos"),
                quantity=quantity, rate=rate, group=group, pick_from=pick_from,
                stock_item=None if pick_from else find_stock(name, make))
            position += 1

        for item in main_items:
            add(item.get("name", ""), item.get("make"), item.get("unit"), services.to_decimal(item.get("quantity"), ZERO),
                services.to_decimal(item.get("rate"), ZERO))
        for name, keyword, quantity, rate, make in VARIANT_ITEMS:
            add(name, make, "Nos", quantity, rate, pick_from=keyword)

        bos_items = list(BosItem.objects.all())
        if bos_items:
            for b in bos_items:
                quantity = b.per_panel * panel_count if b.per_panel > 0 and panel_count > 0 else b.quantity      # J bolt: 4 per panel
                add(b.name, b.make, b.unit, quantity, b.rate, group=Line.BOS)
        elif bos_quoted > 0:
            add("BOS items (lot)", "", "Lot", Decimal("1"), bos_quoted, group=Line.BOS)
        auto_decide(customer)

        Labour.objects.create(customer=customer, kind="structure", label="Structure wage",
                              amount=services.to_decimal(data.get("structure_wage"), ZERO))
        Labour.objects.create(customer=customer, kind="electrical", label="Electrical work",
                              amount=services.to_decimal(data.get("electrical_work"), ZERO))
    return customer


def create_manual_customer(post):
    """A customer whose quotation was NOT made in our quotation website: only the details are typed; the materials are added one by one
    (with the item list as help).  The BOS kit, End cap and MC4 lines and the two labour rows are added like for any customer."""
    customer = Customer.objects.create(
        offer_no=(post.get("offer_no", "").strip() or "Own quotation")[:100], name=post.get("name", "").strip()[:200],
        person=post.get("person", "").strip()[:200], phone=post.get("phone", "").strip()[:50],
        location=post.get("location", "").strip()[:200], system_size=post.get("system_size", "").strip()[:20])
    position = 0
    for name, keyword, quantity, rate, make in VARIANT_ITEMS:
        Line.objects.create(customer=customer, position=position, name=name, make=make, unit="Nos", quantity=quantity, rate=rate, pick_from=keyword)
        position += 1
    for b in BosItem.objects.all():
        Line.objects.create(customer=customer, position=position, group=Line.BOS, name=b.name, make=b.make, unit=b.unit,
                            quantity=b.quantity, rate=b.rate, stock_item=find_stock(b.name, b.make))
        position += 1
    Labour.objects.create(customer=customer, kind="structure", label="Structure wage", amount=ZERO)
    Labour.objects.create(customer=customer, kind="electrical", label="Electrical work", amount=ZERO)
    return customer


SPEC_FIELDS = ("system_size", "phase", "panel_company", "panel_type", "panel_watts", "panel_watts_max", "panel_quantity", "dcr",
               "inverter_kw", "structure", "dc_cable_m", "dc_cable_sqmm", "ac_wire_m")
CUSTOMER_FIELDS = ("name", "person", "offer_no", "phone", "location")


def _specs_page(request, project, store, fields=None, specs=None, warnings=None, error=""):
    """The page where the specifications read from a PDF (or typed) are checked before the quotation website works the materials out."""
    specs = dict(specs or {})
    notes = list(warnings or [])
    try:
        watts = Decimal(specs.get("panel_watts_max") or specs.get("panel_watts") or "0")
        count, size = Decimal(specs.get("panel_quantity") or "0"), Decimal(specs.get("system_size") or "0")
        if watts and count and size and abs(watts * count / 1000 - size) / size > Decimal("0.2"):
            notes.append("%s panels x %s W = %s kW, but the system size is %s kW. Check which one is right." % (
                count.normalize(), watts.normalize(), (watts * count / 1000).quantize(Decimal("0.1")).normalize(), size.normalize()))
    except Exception:
        pass
    return render(request, "store/pdf_specs.html", {"fields": fields or {}, "specs": specs, "warnings": notes, "error": error,
                                                   "project": project, "store": store})


def create_customer_from_specs(post):
    """Asks the quotation website to work the quotation out from the specifications, then makes the customer from its answer."""
    params = {name: post.get("spec_" + name, "").strip() for name in SPEC_FIELDS}
    data = services.estimate(params)
    if not data.get("items"):
        raise services.QuotationApiError("The quotation website returned no materials for those specifications.")
    data.update(id="", offer_no=post.get("offer_no", "").strip()[:100] or "Own quotation", business_name=post.get("name", "").strip(),
                customer_name=post.get("person", "").strip(), phone=post.get("phone", "").strip(), location=post.get("location", "").strip())
    return create_customer(data), data


def refresh_per_panel(customer):
    """For a customer typed in by hand: items that go with the panels (J bolt: 4 per panel) follow the number of panels on the list."""
    if customer.quotation_id is not None or customer.status == Customer.DONE:
        return
    panels = sum((l.quantity for l in customer.lines.filter(group=Line.MAIN) if "panel" in l.name.lower() and "inverter" not in l.name.lower()), ZERO)
    if panels <= 0:
        return
    for bos in BosItem.objects.filter(per_panel__gt=0):
        customer.lines.filter(group=Line.BOS, name__iexact=bos.name).exclude(store_status=Line.DONE).update(quantity=bos.per_panel * panels)


@login_required
def import_quotation(request):
    project, store = _roles(request)
    if not project:
        return HttpResponseForbidden("Only the Project person can bring in a quotation.")
    error, results = "", []
    query = request.GET.get("q", "").strip()

    if request.method == "POST":
        if request.FILES.get("pdf"):
            try:
                result = pdf_import.read_specs(request.FILES["pdf"])
            except pdf_import.PdfReadError as problem:
                messages.error(request, str(problem))
                return redirect("import")
            return _specs_page(request, project, store, result["fields"], result["specs"], result["warnings"])
        if request.POST.get("specs_create"):
            fields = {name: request.POST.get(name, "").strip() for name in CUSTOMER_FIELDS}
            specs = {name: request.POST.get("spec_" + name, "").strip() for name in SPEC_FIELDS}
            if not fields["name"]:
                return _specs_page(request, project, store, fields, specs, error="Type the customer name.")
            try:
                customer, data = create_customer_from_specs(request.POST)
            except services.QuotationApiError as problem:
                return _specs_page(request, project, store, fields, specs, error=str(problem))
            if data.get("panel_used"):
                messages.info(request, "Panel taken from the quotation website's rate sheet: %s." % data["panel_used"])
            for warning in data.get("warnings", []):
                messages.warning(request, "Quotation website: %s" % warning)
            messages.success(request, "%s created from the specifications with %d items. Rates are the quotation website's rates; the warehouse decides what is taken from stock." % (
                customer.name, customer.lines.filter(group=Line.MAIN).count()))
            return redirect("customer", pk=customer.pk)
        if request.POST.get("manual"):
            if not request.POST.get("name", "").strip():
                messages.error(request, "Type the customer name.")
                return redirect("import")
            customer = create_manual_customer(request.POST)
            messages.success(request, "%s created. Add the materials of the quotation with 'Add an item'." % customer.name)
            return redirect("customer", pk=customer.pk)
        data, quotation_id = None, request.POST.get("quotation_id", "").strip()
        pasted = request.POST.get("json_text", "").strip()
        try:
            if pasted:
                data = json.loads(pasted)
            elif quotation_id.isdigit():
                existing = Customer.objects.filter(quotation_id=int(quotation_id)).first()
                if existing:
                    messages.info(request, "That quotation is already a customer here.")
                    return redirect("customer", pk=existing.pk)
                data = services.get_quotation(int(quotation_id))
            else:
                error = "Choose a quotation."
            if data is not None:
                customer = create_customer(data)
                messages.success(request, "%s brought in with %d items. Now choose warehouse or purchase for each." % (customer.name, customer.lines.count()))
                return redirect("customer", pk=customer.pk)
        except (services.QuotationApiError, ValueError, AttributeError) as problem:
            error = str(problem) or "Could not read that quotation."
    elif request.GET.get("specs"):
        return _specs_page(request, project, store, {}, {"phase": "single", "structure": "roof"})
    else:
        try:
            results = services.search_quotations(query)
        except services.QuotationApiError as problem:
            error = str(problem)

    return render(request, "store/import.html", {"results": results, "error": error, "query": query, "project": project, "store": store})


# ---------------------------------------------------------------------------------------------- the material sheet
def _rows(customer):
    stocks = list(Stock.objects.all())
    reserved = reserved_by_stock()
    rows = []
    for line in customer.lines.select_related("stock_item"):
        stock = line.stock_item
        free = None
        if stock is not None:
            mine = line.quantity if (line.source == Line.WAREHOUSE and line.store_status != Line.DONE) else ZERO
            free = stock.quantity - (reserved.get(stock.pk, ZERO) - mine)       # what the others have not already promised
        enough = None if stock is None else free >= line.quantity
        options = [s for s in stocks if line.pick_from and line.pick_from in ("%s %s" % (s.name, s.make)).lower()] if line.pick_from else []
        rows.append({"line": line, "stock": stock, "enough": enough, "free": free, "amount": line.amount, "options": options})
    return rows


def _groups(rows):
    groups = []
    for source, title in ((Line.WAREHOUSE, "Take from warehouse"), (Line.PURCHASE, "Purchase"), (Line.UNDECIDED, "Not decided yet")):
        chosen = [r for r in rows if r["line"].source == source]
        if chosen:
            groups.append({"source": source, "title": title, "rows": chosen, "total": sum((r["amount"] for r in chosen), ZERO)})
    return groups


def kit_value(lines, attr):
    """The one value all BOS lines share ("mixed" when some differ, "" when there are none)."""
    values = {getattr(l, attr) for l in lines if l.quantity > 0}
    return values.pop() if len(values) == 1 else ("mixed" if values else "")


def _apply_form(request, customer, project, store):
    """Saves what each role is allowed to change.  Project: name, make, unit, quantity, warehouse / purchase, delete, the labour.
    Store: status, note, which stock item.  Both: the rate (and the warehouse item of End cap / MC4 style lines)."""
    problems = []
    post = request.POST
    with transaction.atomic():
        # the BOS kit has ONE decision (and one store status): when it is changed, every BOS item follows it;
        # an item changed by hand inside the kit is kept when the kit box itself was not touched
        kit = list(customer.lines.filter(group=Line.BOS))
        kit_source = post.get("bos_source", "")
        kit_status = post.get("bos_status", "")
        if not (project and kit_source in dict(Line.SOURCES) and kit_source != kit_value(kit, "source")):
            kit_source = ""
        if not (store and kit_status in dict(Line.STORE_STATUS) and kit_status != kit_value(kit, "store_status")):
            kit_status = ""
        for line in customer.lines.select_related("stock_item"):
            p = "line-%d-" % line.id
            in_kit = line.group == Line.BOS
            if project and post.get(p + "delete"):
                release_stock(line)
                line.delete()
                continue
            if project:
                if post.get(p + "name", "").strip():
                    line.name = post[p + "name"].strip()[:250]
                if p + "make" in post:
                    line.make = post[p + "make"].strip()[:100]
                if post.get(p + "unit", "").strip():
                    line.unit = post[p + "unit"].strip()[:20]
                if p + "quantity" in post:
                    quantity = services.to_decimal(post[p + "quantity"])
                    if quantity is None or quantity < 0:
                        problems.append("%s: the quantity is not a valid number." % line.name)
                    else:
                        line.quantity = quantity
                if in_kit and kit_source:
                    line.source = kit_source
                elif post.get(p + "source") in dict(Line.SOURCES):
                    line.source = post[p + "source"]
            if p + "rate" in post and (project or store):
                rate = services.to_decimal(post[p + "rate"])
                if rate is None or rate < 0:
                    problems.append("%s: the rate is not a valid number." % line.name)
                else:
                    line.rate = rate
            if store:
                if in_kit and kit_status:
                    line.store_status = kit_status
                elif post.get(p + "store_status") in dict(Line.STORE_STATUS):
                    line.store_status = post[p + "store_status"]
                if p + "store_note" in post:
                    line.store_note = post[p + "store_note"].strip()[:250]
            if (store or (project and line.pick_from)) and p + "stock_item" in post:
                raw = post[p + "stock_item"]
                chosen = Stock.objects.filter(pk=int(raw)).first() if raw.isdigit() else None
                changed = (chosen.pk if chosen else None) != line.stock_item_id
                line.stock_item = chosen
                if changed and chosen and line.pick_from:         # End cap / MC4: picking a warehouse item fills in its make, unit and rate
                    line.make = (chosen.make or chosen.name)[:100]
                    line.unit = chosen.unit[:20] or line.unit
                    if chosen.rate > 0:
                        line.rate = chosen.rate
            sync_stock(line)

        if project:
            for labour in customer.labour.all():
                p = "labour-%d-" % labour.id
                if p + "amount" in post:
                    amount = services.to_decimal(post[p + "amount"])
                    if amount is None or amount < 0:
                        problems.append("%s: the amount is not a valid number." % labour.label)
                    else:
                        labour.amount = amount
                if post.get(p + "worker_type") in dict(Labour.TYPES):
                    labour.worker_type = post[p + "worker_type"]
                if p + "worker_name" in post:
                    labour.worker_name = post[p + "worker_name"].strip()[:100]
                if labour.worker_type != Labour.CONTRACT:
                    labour.advance = ZERO
                elif p + "advance" in post:
                    advance = services.to_decimal(post[p + "advance"] or "0")
                    if advance is None or advance < 0:
                        problems.append("%s: the advance is not a valid number." % labour.label)
                    elif advance > labour.amount:
                        problems.append("%s: the advance cannot be more than the amount." % labour.label)
                    else:
                        labour.advance = advance
                labour.save()
    return problems


def _add_line(request, customer, group):
    prefix = "new_bos_" if group == Line.BOS else "new_"
    name = request.POST.get(prefix + "name", "").strip()
    if not name:
        messages.error(request, "Type the item name to add it.")
        return
    last = customer.lines.order_by("-position").first()
    Line.objects.create(
        customer=customer, group=group, position=(last.position + 1) if last else 0, name=name[:250],
        make=request.POST.get(prefix + "make", "").strip()[:100],
        unit=(request.POST.get(prefix + "unit", "").strip() or "Nos")[:20],
        quantity=services.to_decimal(request.POST.get(prefix + "quantity"), Decimal("1")),
        rate=services.to_decimal(request.POST.get(prefix + "rate"), ZERO),
        stock_item=find_stock(name, request.POST.get(prefix + "make", "")))
    messages.success(request, "%s added." % name)


@login_required
def customer_detail(request, pk):
    project, store = _roles(request)
    customer = get_object_or_404(Customer, pk=pk)
    if not (project or store):
        return HttpResponseForbidden("Ask the owner to give you the Project or the Store role.")
    if customer.status == Customer.DRAFT and not project:
        return HttpResponseForbidden("This list has not been sent to the store yet.")

    relink(customer)
    if request.method == "GET" and project and customer.status == Customer.DRAFT:
        merge_balances(customer)         # new stock arrived for a split item: join it up again
        auto_decide(customer)            # every item still "Not decided" follows the warehouse by itself
    if request.method == "POST":
        action = request.POST.get("action", "save")
        problems = _apply_form(request, customer, project, store)
        for problem in problems:
            messages.error(request, problem)

        if project and request.POST.get("bos_source") == "auto":      # the kit: warehouse where it is available, the rest purchase
            merge_balances(customer, group=Line.BOS, force=True)
            customer.lines.filter(group=Line.BOS).exclude(store_status=Line.DONE).exclude(store_note__startswith="balance") \
                .update(source=Line.UNDECIDED)
            done = auto_decide(customer, group=Line.BOS)
            messages.success(request, "BOS kit: %d from the warehouse, %d to purchase." % (done["warehouse"], done["purchase"]))

        if action == "add_line" and project:
            _add_line(request, customer, Line.MAIN)
            refresh_per_panel(customer)
        elif action == "add_bos" and project:
            _add_line(request, customer, Line.BOS)
        elif action in ("all_warehouse", "all_purchase", "all_auto") and project:
            open_lines = customer.lines.filter(quantity__gt=0).exclude(store_status=Line.DONE)
            if action == "all_auto":
                merge_balances(customer, force=True)
                open_lines.exclude(store_note__startswith="balance").update(source=Line.UNDECIDED)
                done = auto_decide(customer)
                messages.success(request, "Everything decided from the warehouse: %d warehouse, %d purchase%s." % (
                    done["warehouse"], done["purchase"], (", %d split" % done["split"]) if done["split"] else ""))
            else:
                chosen = Line.WAREHOUSE if action == "all_warehouse" else Line.PURCHASE
                count = open_lines.update(source=chosen)
                messages.success(request, "All %d items set to %s." % (count, "take from warehouse" if chosen == Line.WAREHOUSE else "purchase"))
        elif action == "auto" and project:
            merge_balances(customer, force=True)
            done = auto_decide(customer)
            messages.success(request, "Decided from the warehouse: %d warehouse, %d purchase%s." % (
                done["warehouse"], done["purchase"], (", %d item%s split" % (done["split"], "" if done["split"] == 1 else "s")) if done["split"] else ""))
        elif action == "send" and project:
            if any(l.source == Line.UNDECIDED for l in _needed(list(customer.lines.all()))):
                messages.error(request, "Choose warehouse or purchase for every item (BOS items too) before sending it to the store.")
            else:
                customer.status = Customer.SENT
                customer.sent_at = timezone.now()
                customer.save()
                messages.success(request, "Sent to the store.")
        elif action == "complete":
            if any(l.store_status != Line.DONE for l in _needed(list(customer.lines.all()))):
                messages.error(request, "Every item must be Done before the customer is completed.")
            else:
                customer.status = Customer.DONE
                customer.save()
                messages.success(request, "Marked completed.")
        elif action == "reopen" and project:
            customer.status = Customer.SENT
            customer.save()
        else:
            if not problems:
                messages.success(request, "Saved.")
        return redirect("customer", pk=customer.pk)

    rows = _rows(customer)
    main_rows = [r for r in rows if r["line"].group != Line.BOS]
    bos_rows = [r for r in rows if r["line"].group == Line.BOS]
    groups = _groups(main_rows)
    needed = [r for r in rows if r["line"].quantity > 0]
    bos_needed = [r for r in bos_rows if r["line"].quantity > 0]
    bos_short = []                       # BOS items that are not fully in the warehouse: shown in the red box for the project person
    for r in bos_rows:
        l, s = r["line"], r["stock"]
        if l.quantity <= 0 or l.source == Line.WAREHOUSE:
            continue
        if s is None:
            reason = "not in the warehouse list"
        elif s.quantity <= 0:
            reason = "out of stock"
        elif l.store_note.startswith("balance"):
            reason = "only part is in the warehouse: this is the balance"
        else:
            reason = "not enough in the warehouse (have %s)" % s.quantity.normalize()
        bos_short.append({"name": l.name, "quantity": l.quantity.normalize(), "unit": l.unit, "reason": reason})
    return render(request, "store/customer.html", {
        "bos_short": bos_short, "catalog": _catalog() if project else [],
        "customer": customer, "groups": groups, "project": project, "store": store,
        "stocks": Stock.objects.all(),
        "bos_rows": bos_rows, "bos_total": sum((r["amount"] for r in bos_rows), ZERO),
        "kit_source": kit_value([r["line"] for r in bos_rows], "source"),
        "kit_status": kit_value([r["line"] for r in bos_rows], "store_status"),
        "bos_counts": {"warehouse": sum(1 for r in bos_needed if r["line"].source == Line.WAREHOUSE),
                       "purchase": sum(1 for r in bos_needed if r["line"].source == Line.PURCHASE),
                       "undecided": sum(1 for r in bos_needed if r["line"].source == Line.UNDECIDED),
                       "done": sum(1 for r in bos_needed if r["line"].store_status == Line.DONE)},
        "labour": list(customer.labour.all()),
        "purchase_total": sum((r["amount"] for r in rows if r["line"].source == Line.PURCHASE), ZERO),
        "warehouse_total": sum((r["amount"] for r in rows if r["line"].source == Line.WAREHOUSE), ZERO),
        "undecided": sum(1 for r in needed if r["line"].source == Line.UNDECIDED),
        "short": sum(1 for r in needed if r["line"].store_status == Line.SHORT),
        "source_choices": Line.SOURCES, "status_choices": Line.STORE_STATUS,
    })


@login_required
def export_csv(request, pk, source):
    project, store = _roles(request)
    if not (project or store):
        return HttpResponseForbidden("No role.")
    customer = get_object_or_404(Customer, pk=pk)
    if source not in (Line.WAREHOUSE, Line.PURCHASE):
        raise Http404()
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="%s-%s-list.csv"' % (customer.offer_no.replace("/", "-") or customer.pk, source)
    writer = csv.writer(response)
    writer.writerow(["Item", "Make", "Quantity", "Unit", "Rate", "Amount", "Status", "Note"])
    for line in customer.lines.filter(source=source, quantity__gt=0):
        writer.writerow([line.name, line.make, line.quantity, line.unit, line.rate, line.amount, line.get_store_status_display(), line.store_note])
    return response


# ---------------------------------------------------------------------------------------------- the warehouse
def apply_stock_rate(stock, rate):
    """A new warehouse rate: every open list that takes this item from the warehouse follows it (items already Done keep their rate).
    Returns how many list items changed."""
    return Line.objects.filter(stock_item=stock, source=Line.WAREHOUSE).exclude(store_status=Line.DONE) \
        .exclude(customer__status=Customer.DONE).update(rate=rate)


def _catalog():
    path = Path(__file__).resolve().parent / "catalog.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


@login_required
def stock_list(request):
    project, store = _roles(request)
    if not (project or store):
        return HttpResponseForbidden("Ask the owner to give you the Project or the Store role.")
    if request.method == "POST":
        if not store:
            return HttpResponseForbidden("Only the Store person updates the warehouse.")
        if request.POST.get("action") == "add":
            name = request.POST.get("new_name", "").strip()
            make = request.POST.get("new_make", "").strip()[:100]
            quantity = services.to_decimal(request.POST.get("new_quantity"), ZERO)
            rate = services.to_decimal(request.POST.get("new_rate"), None) if request.POST.get("new_rate", "").strip() else None
            if not name:
                messages.error(request, "Type the item name.")
            elif quantity is None or quantity < 0 or (rate is not None and rate < 0):
                messages.error(request, "Quantity and rate must be numbers, not negative.")
            else:
                existing = next((s for s in Stock.objects.all() if _norm(s.name) == _norm(name) and _norm(s.make) == _norm(make)), None)
                if existing:                       # the same item and make: the quantity is ADDED to what is there
                    existing.quantity += quantity
                    changed = 0
                    if rate is not None and rate != existing.rate:
                        existing.rate = rate
                        changed = apply_stock_rate(existing, rate)
                    if request.POST.get("new_unit", "").strip():
                        existing.unit = request.POST["new_unit"].strip()[:20]
                    existing.save()
                    messages.success(request, "%s: %s added, now %s in the warehouse.%s" % (
                        existing.label, quantity.normalize(), existing.quantity.normalize(),
                        (" The new rate was applied to %d open list item%s." % (changed, "" if changed == 1 else "s")) if changed else ""))
                else:
                    Stock.objects.create(name=name[:200], make=make, unit=(request.POST.get("new_unit", "").strip() or "Nos")[:20],
                                         quantity=quantity, rate=rate if rate is not None else ZERO)
                    messages.success(request, "%s added to the warehouse." % (("%s - %s" % (name, make)) if make else name))
        else:
            updated = 0
            with transaction.atomic():
                for stock in Stock.objects.all():
                    p = "stock-%d-" % stock.id
                    if p + "quantity" not in request.POST:
                        continue
                    if request.POST.get(p + "delete"):
                        stock.delete()
                        continue
                    quantity = services.to_decimal(request.POST.get(p + "quantity"))
                    rate = services.to_decimal(request.POST.get(p + "rate"))
                    if quantity is None or quantity < 0 or rate is None or rate < 0:
                        messages.error(request, "%s: quantity and rate must be numbers, not negative." % stock.name)
                        continue
                    if rate != stock.rate:
                        updated += apply_stock_rate(stock, rate)
                    stock.quantity, stock.rate = quantity, rate
                    if request.POST.get(p + "name", "").strip():
                        stock.name = request.POST[p + "name"].strip()[:200]
                    if p + "make" in request.POST:
                        stock.make = request.POST[p + "make"].strip()[:100]
                    if request.POST.get(p + "unit", "").strip():
                        stock.unit = request.POST[p + "unit"].strip()[:20]
                    stock.save()
            messages.success(request, "Warehouse saved." + (" The new rate was applied to %d open list item%s." % (updated, "" if updated == 1 else "s") if updated else ""))
        return redirect("stock")
    return render(request, "store/stock.html", {"stocks": Stock.objects.all(), "project": project, "store": store, "catalog": _catalog()})
