from decimal import Decimal

from django.db import models
from django.utils import timezone


class Stock(models.Model):
    """One item in the warehouse.  Kept up to date by the Store person."""
    name = models.CharField(max_length=200)
    make = models.CharField(max_length=100, blank=True)        # the make / size, e.g. "4CX10 - 19mm": the same name can have several
    unit = models.CharField(max_length=20, default="Nos")
    quantity = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    rate = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "make"]

    @property
    def label(self):
        return "%s - %s" % (self.name, self.make) if self.make else self.name

    def __str__(self):
        return self.label


class BosItem(models.Model):
    """The small BOS items (sockets, glands, clamps ...) added to every customer's list.  Loaded by `load_bos` from the quotation work-out sheet; editable in /admin/."""
    name = models.CharField(max_length=250)
    make = models.CharField(max_length=100, blank=True)
    unit = models.CharField(max_length=20, default="Nos")
    quantity = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    rate = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    per_panel = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0"))      # > 0: quantity = this x number of panels (J bolt: 4)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return "%s %s" % (self.name, self.make)


class Customer(models.Model):
    """A quotation that became a customer: its material list is prepared and sent to the store."""
    DRAFT, SENT, DONE = "draft", "sent", "done"
    STATUS = [(DRAFT, "Project person is preparing the list"), (SENT, "With the store"), (DONE, "Completed")]

    quotation_id = models.PositiveIntegerField(null=True, blank=True)       # the id in the quotation website
    offer_no = models.CharField(max_length=100, blank=True)
    name = models.CharField(max_length=200)
    person = models.CharField(max_length=200, blank=True)                   # "Quotation For" in the quotation website
    phone = models.CharField(max_length=50, blank=True)
    location = models.CharField(max_length=200, blank=True)
    system_size = models.CharField(max_length=20, blank=True)
    bos_quoted = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))      # the BOS amount inside the quotation
    quoted_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))    # the final price of the quotation (with GST), taken from the quotation website
    agreed_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))   # typed by the accountant ONLY when the customer pays a different amount
    status = models.CharField(max_length=10, choices=STATUS, default=DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def total_due(self):
        """What the customer has to pay in all: the amount the accountant typed (a negotiated price), else the quotation's final price."""
        return self.agreed_amount if self.agreed_amount > 0 else self.quoted_total

    def __str__(self):
        return "%s (%s)" % (self.name, self.offer_no)


class Line(models.Model):
    """One material of a customer."""
    UNDECIDED, WAREHOUSE, PURCHASE = "undecided", "warehouse", "purchase"
    SOURCES = [(UNDECIDED, "Not decided"), (WAREHOUSE, "Take from warehouse"), (PURCHASE, "Purchase")]
    PENDING, DONE, SHORT = "pending", "done", "short"
    STORE_STATUS = [(PENDING, "Waiting"), (DONE, "Done"), (SHORT, "Not enough / not available")]

    MAIN, BOS = "main", "bos"
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="lines")
    group = models.CharField(max_length=10, default=MAIN)            # "bos" = shown inside the single expandable BOS line
    pick_from = models.CharField(max_length=30, blank=True)          # e.g. "end cap", "mc4": the warehouse items containing this text can be chosen
    position = models.PositiveIntegerField(default=0)
    name = models.CharField(max_length=250)
    make = models.CharField(max_length=100, blank=True)
    unit = models.CharField(max_length=20, default="Nos")
    quantity = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    rate = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))

    source = models.CharField(max_length=10, choices=SOURCES, default=UNDECIDED)          # decided by the Project person
    store_status = models.CharField(max_length=10, choices=STORE_STATUS, default=PENDING)  # updated by the Store person
    store_note = models.CharField(max_length=250, blank=True)
    stock_item = models.ForeignKey(Stock, null=True, blank=True, on_delete=models.SET_NULL, related_name="lines")

    # what this line has taken out of the warehouse stock (so a change can be undone exactly)
    applied_to = models.ForeignKey(Stock, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    applied_qty = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))

    class Meta:
        ordering = ["position", "id"]

    @property
    def amount(self):
        return (self.quantity or Decimal("0")) * (self.rate or Decimal("0"))

    def __str__(self):
        return self.name


class Labour(models.Model):
    """The structure wage / electrical work of a customer, and who does it: company staff or a contractor (with an advance)."""
    STAFF, CONTRACT = "staff", "contract"
    TYPES = [(STAFF, "Company staff"), (CONTRACT, "Contract")]

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="labour")
    kind = models.CharField(max_length=20)                           # "structure" or "electrical"
    label = models.CharField(max_length=60)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))      # from the quotation, editable
    worker_type = models.CharField(max_length=10, choices=TYPES, default=STAFF)
    worker_name = models.CharField(max_length=100, blank=True)
    advance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))      # only for a contractor

    class Meta:
        ordering = ["id"]

    @property
    def balance(self):
        return (self.amount or Decimal("0")) - (self.advance or Decimal("0"))

    def __str__(self):
        return "%s - %s" % (self.customer, self.label)


class Payment(models.Model):
    """Money received from a customer.  Typed in by the Accounts person."""
    CASH, BANK, UPI, CHEQUE, OTHER = "cash", "bank", "upi", "cheque", "other"
    MODES = [(CASH, "Cash"), (BANK, "Bank transfer"), (UPI, "UPI"), (CHEQUE, "Cheque"), (OTHER, "Other")]

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="payments")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_on = models.DateField(default=timezone.localdate)
    mode = models.CharField(max_length=10, choices=MODES, default=BANK)
    note = models.CharField(max_length=200, blank=True)             # cheque no., transaction id, "advance", "final" ...
    recorded_by = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-paid_on", "-id"]

    def __str__(self):
        return "%s  %s  %s" % (self.customer, self.paid_on, self.amount)
