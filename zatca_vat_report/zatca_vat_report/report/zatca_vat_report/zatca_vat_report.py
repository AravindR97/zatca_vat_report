# Copyright (c) 2026, Aravind R and contributors
# For license information, please see license.txt

import frappe
from frappe.utils import flt


def execute(filters=None):
    if not filters:
        filters = {}

    if not filters.get("from_date") or not filters.get("to_date"):
        return

    columns = get_columns()
    data = get_data(filters)

    return columns, data


def get_columns():
    return [
        {"fieldname": "title", "label": "Title", "fieldtype": "Data", "width": 300},
        {"fieldname": "amount", "label": "Amount", "fieldtype": "Currency", "width": 150},
        {"fieldname": "adjustment", "label": "Adjustment", "fieldtype": "Currency", "width": 150},
        {"fieldname": "net_amount", "label": "Net Amount", "fieldtype": "Currency", "width": 150},
        {"fieldname": "net_vat_amount", "label": "Net VAT Amount", "fieldtype": "Currency", "width": 150},
    ]

from collections import defaultdict


def get_detail_link(label, section, group_name, filters, bucket=None):
    """Build a clickable <a href> link to ZATCA VAT Report Detail.
    All filter values are encoded in the URL so Frappe's body click handler
    can populate frappe.route_options automatically (same pattern as DCR Report).
    group_name is optional (e.g. for Bayan section which has no account group).
    """
    from urllib.parse import urlencode, quote
    from frappe.utils import get_url

    params = {
        "section": section,
        "from_date": filters.get("from_date", ""),
        "to_date": filters.get("to_date", ""),
    }
    if group_name:
        params["group_label"] = group_name
    if filters.get("company"):
        params["company"] = filters["company"]
    if bucket and section == "Purchase":
        params["bucket"] = bucket

    query_string = urlencode({k: v for k, v in params.items() if v})
    report_name = quote("ZATCA VAT Report Detail", safe="")
    url = get_url(f"/app/query-report/{report_name}?{query_string}")
    return f'<a href="{url}">{frappe.utils.escape_html(label)}</a>'


def get_account_group_map():
    settings = frappe.get_single("ZATCA VAT Report Settings")

    result = {
        "Sales": {},
        "Purchase": {},
        "Expense": {},
    }

    # Sales groups
    for row in settings.account_groups:
        group = frappe.get_doc("ZATCA Account Group", row.account_group)
        result["Sales"][group.account_group_label] = {
            "group_name": group.name,
            "accounts": [acc.account for acc in group.linked_accounts],
            "tax_rate": row.tax_rate
        }

    # Purchase groups
    for row in settings.purchase_account_groups:
        group = frappe.get_doc("ZATCA Account Group", row.account_group)
        result["Purchase"][group.account_group_label] = {
            "group_name": group.name,
            "accounts": [acc.account for acc in group.linked_accounts],
            "tax_rate": row.tax_rate
        }

    # Expense groups
    for row in settings.expense_account_groups:
        group = frappe.get_doc("ZATCA Account Group", row.account_group)
        result["Expense"][group.account_group_label] = {
            "group_name": group.name,
            "accounts": [acc.account for acc in group.linked_accounts],
            "tax_rate": row.tax_rate
        }

    return result

def get_expense_vat_from_journal_entries(filters, accounts):
    conditions = []
    values = {}

    # Base conditions
    conditions.append("je.docstatus = 1")
    conditions.append("je.is_system_generated = 0")
    conditions.append("je.posting_date BETWEEN %(from_date)s AND %(to_date)s")

    if filters.get("company"):
        conditions.append("je.company = %(company)s")
        values["company"] = filters["company"]

    if accounts:
        conditions.append("jea.account IN %(accounts)s")
        values["accounts"] = tuple(accounts)

    values.update(filters)

    query = f"""
        SELECT
            IFNULL(
                SUM(
                    CASE
                        WHEN jea.debit > 0 THEN jea.debit
                        WHEN jea.credit > 0 THEN -jea.credit
                        ELSE 0
                    END
                ), 0
            ) AS net_amount
        FROM `tabJournal Entry` je
        INNER JOIN `tabJournal Entry Account` jea
            ON jea.parent = je.name
        WHERE
            {' AND '.join(conditions)}
    """

    result = frappe.db.sql(query, values, as_dict=True)
    return result[0].get("net_amount", 0) or 0


def get_expense_vat_from_expense_claims(filters, accounts):
    conditions = []
    values = {}

    # Base conditions
    conditions.append("ec.docstatus = 1")
    conditions.append("ec.posting_date BETWEEN %(from_date)s AND %(to_date)s")

    if filters.get("company"):
        conditions.append("ec.company = %(company)s")
        values["company"] = filters["company"]

    if accounts:
        conditions.append("ect.account_head IN %(accounts)s")
        values["accounts"] = tuple(accounts)

    values.update(filters)

    query = f"""
        SELECT
            IFNULL(
                SUM(ABS(ect.tax_amount)), 0
            ) AS net_amount
        FROM `tabExpense Claim` ec
        INNER JOIN `tabExpense Taxes and Charges` ect
            ON ect.parent = ec.name
        WHERE
            {' AND '.join(conditions)}
    """

    result = frappe.db.sql(query, values, as_dict=True)
    return result[0].get("net_amount", 0) or 0


def get_taxable_summary(doctype, tax_table, filters, accounts, tax_rate, is_sales=True):
    """
    Calculate actual taxable amount with optimized logic:
    
    1. Check if invoice has multiple tax rows
    2. If single row: Use invoice net_total directly (simple case)
    3. If multiple rows: Use reverse calculation (tax_amount / tax_rate)
    4. For zero-rated with multiple rows: Subtract non-zero shares from total
    """
    conditions = []
    values = {}

    if filters.get("company"):
        conditions.append("inv.company = %(company)s")
        values["company"] = filters["company"]

    conditions.append("inv.docstatus = 1")
    conditions.append("inv.posting_date BETWEEN %(from_date)s AND %(to_date)s")

    if is_sales and frappe.db.exists("DocType", "ZATCA Integration Log"):
        conditions.append("""
            inv.name NOT IN (
                SELECT zil.invoice_reference
                FROM `tabZATCA Integration Log` zil
                WHERE zil.status = 'Rejected'
            )
        """)

    # if not is_sales:
    #     conditions.append("(inv.bill_date IS NULL OR inv.bill_date >= %(from_date)s)")

    values.update(filters)

    account_condition = ""
    if accounts:
        account_condition = "AND tax.account_head IN %(accounts)s"
        values["accounts"] = tuple(accounts)

    # ---------------- ZERO RATED LOGIC (tax_rate = 0) ----------------
    if tax_rate == 0:
        query = f"""
            SELECT
                inv.name AS invoice_name,
                inv.base_net_total,
                inv.is_return,
                
                -- Count tax rows for this invoice
                (SELECT COUNT(*) 
                 FROM `{tax_table}` t2 
                 INNER JOIN `tabAccount` a2 ON t2.account_head = a2.name
                 WHERE t2.parent = inv.name AND a2.account_type = 'Tax'
                ) AS tax_row_count,
                
                -- Calculate total taxable amount at non-zero rates (only if multiple rows)
                IFNULL((
                    SELECT SUM(
                        CASE 
                            WHEN acc_master.tax_rate IS NOT NULL AND acc_master.tax_rate > 0 
                            THEN ABS(t.tax_amount) / (acc_master.tax_rate / 100)
                            ELSE 0
                        END
                    )
                    FROM `{tax_table}` t
                    INNER JOIN `tabAccount` acc ON t.account_head = acc.name
                    LEFT JOIN `tabAccount` acc_master ON acc_master.name = t.account_head
                    WHERE t.parent = inv.name 
                      AND acc.account_type = 'Tax'
                      AND acc_master.tax_rate IS NOT NULL 
                      AND acc_master.tax_rate > 0
                ), 0) AS non_zero_taxed_amount
                
            FROM `{tax_table}` tax
            INNER JOIN `{doctype}` inv ON tax.parent = inv.name
            INNER JOIN `tabAccount` acc ON tax.account_head = acc.name
            WHERE
                acc.account_type = 'Tax'
                {account_condition}
                AND {' AND '.join(conditions)}
            GROUP BY inv.name
        """
        
        results = frappe.db.sql(query, values, as_dict=True)
        
        amount = 0
        adjustment = 0
        
        for row in results:
            if row.get("tax_row_count", 0) == 1:
                # Single tax row: Use invoice total directly
                zero_rated_amount = row.get("base_net_total", 0)
            else:
                # Multiple tax rows: Subtract non-zero shares from total
                zero_rated_amount = row.get("base_net_total", 0) - row.get("non_zero_taxed_amount", 0)
                zero_rated_amount = max(zero_rated_amount, 0)
            
            if row.get("is_return", 0) == 0:
                amount += zero_rated_amount
            else:
                adjustment += abs(zero_rated_amount)
        
        return [{"account_head": accounts[0] if accounts else "Zero Rated", 
                 "amount": amount, 
                 "adjustment": adjustment}]

    # ---------------- STANDARD / OTHER RATES (tax_rate > 0) ----------------
    query = f"""
        SELECT
            tax.account_head,

            IFNULL(
                SUM(
                    CASE
                        WHEN inv.is_return = 0 THEN
                            CASE
                                -- Single tax row: Use invoice total
                                WHEN (SELECT COUNT(*) 
                                      FROM `{tax_table}` t2 
                                      INNER JOIN `tabAccount` a2 ON t2.account_head = a2.name
                                      WHERE t2.parent = inv.name AND a2.account_type = 'Tax') = 1
                                THEN inv.base_net_total
                                -- Multiple tax rows: Calculate share using reverse method
                                WHEN acc_master.tax_rate IS NOT NULL AND acc_master.tax_rate > 0
                                THEN ABS(tax.base_tax_amount) / (acc_master.tax_rate / 100)
                                ELSE 0
                            END
                        ELSE 0
                    END
                ), 0
            ) AS amount,

            IFNULL(
                SUM(
                    CASE
                        WHEN inv.is_return = 1 THEN
                            CASE
                                -- Single tax row: Use invoice total
                                WHEN (SELECT COUNT(*) 
                                      FROM `{tax_table}` t2 
                                      INNER JOIN `tabAccount` a2 ON t2.account_head = a2.name
                                      WHERE t2.parent = inv.name AND a2.account_type = 'Tax') = 1
                                THEN ABS(inv.base_net_total)
                                -- Multiple tax rows: Calculate share using reverse method
                                WHEN acc_master.tax_rate IS NOT NULL AND acc_master.tax_rate > 0
                                THEN ABS(tax.base_tax_amount) / (acc_master.tax_rate / 100)
                                ELSE 0
                            END
                        ELSE 0
                    END
                ), 0
            ) AS adjustment

        FROM `{tax_table}` tax
        INNER JOIN `{doctype}` inv ON tax.parent = inv.name
        INNER JOIN `tabAccount` acc ON tax.account_head = acc.name
        LEFT JOIN `tabAccount` acc_master ON acc_master.name = tax.account_head
        WHERE
            acc.account_type = 'Tax'
            {account_condition}
            AND {' AND '.join(conditions)}
            AND acc_master.tax_rate = %(expected_tax_rate)s
        GROUP BY tax.account_head
    """
    
    values["expected_tax_rate"] = tax_rate

    return frappe.db.sql(query, values, as_dict=True)


def get_purchase_vat_split(filters, accounts=None):
    """Split VAT on purchases into stock, expense, and asset based on expense head.

    Logic:
    - Asset: Account with root_type = 'Asset' AND account_type in ('Fixed Asset', 'Capital Work in Progress')
    - Expense: Account with root_type = 'Expense' OR account_type in ('Expense Account', 'Indirect Expense')
    - Purchase (stock-related): Everything else used as expense head on Purchase Invoice
    """
    if filters is None:
        filters = {}

    conditions = [
        "inv.docstatus = 1",
        "inv.posting_date BETWEEN %(from_date)s AND %(to_date)s",
        "(inv.bill_date IS NULL OR inv.bill_date >= %(from_date)s)",
        "COALESCE(inv.custom_bayan_value, 0) = 0",
    ]

    values = {}
    if filters.get("company"):
        conditions.append("inv.company = %(company)s")
        values["company"] = filters["company"]

    values.update(filters)

    account_condition = ""
    if accounts:
        account_condition = "AND tax.account_head IN %(accounts)s"
        values["accounts"] = tuple(accounts)

    where_clause = " AND ".join(conditions)

    # 1) Get taxable base split per invoice by account classification.
    # We classify using effective_account_type (with parent fallback).
    #
    # Buckets:
    # - Asset:   account_type in
    #            ('Fixed Asset', 'Capital Work in Progress',
    #             'Accumulated Depreciation',
    #             'Expenses Included In Asset Valuation',
    #             'Asset Received But Not Billed')
    # - Expense: account_type in
    #            ('Expense Account', 'Direct Expense', 'Indirect Expense',
    #             'Depreciation', 'Service Received But Not Billed',
    #             'Expenses Included In Valuation', 'Chargeable')
    #            OR root_type = 'Expense'
    # - Purchase (stock): account_type in
    #            ('Cost of Goods Sold', 'Stock', 'Stock Adjustment',
    #             'Stock Received But Not Billed')
    base_query = f"""
        SELECT
            inv.name AS invoice,
            inv.is_return,
            SUM(
                CASE
                    WHEN COALESCE(acc.account_type, acc_parent.account_type) IN (
                            'Fixed Asset',
                            'Capital Work in Progress',
                            'Accumulated Depreciation',
                            'Expenses Included In Asset Valuation',
                            'Asset Received But Not Billed'
                         )
                    THEN pii.base_net_amount
                    ELSE 0
                END
            ) AS asset_base,
            SUM(
                CASE
                    WHEN COALESCE(acc.account_type, acc_parent.account_type) IN (
                            'Expense Account',
                            'Direct Expense',
                            'Indirect Expense',
                            'Depreciation',
                            'Service Received But Not Billed',
                            'Expenses Included In Valuation',
                            'Chargeable'
                         )
                         OR COALESCE(acc.root_type, acc_parent.root_type) = 'Expense'
                    THEN pii.base_net_amount
                    ELSE 0
                END
            ) AS expense_base,
            SUM(
                CASE
                    WHEN COALESCE(acc.account_type, acc_parent.account_type) IN (
                        'Cost of Goods Sold',
                        'Stock',
                        'Stock Adjustment',
                        'Stock Received But Not Billed'
                    )
                    THEN pii.base_net_amount
                    ELSE 0
                END
            ) AS purchase_base
        FROM `tabPurchase Invoice` inv
        INNER JOIN `tabPurchase Invoice Item` pii
            ON pii.parent = inv.name
        LEFT JOIN `tabAccount` acc
            ON acc.name = pii.expense_account
        LEFT JOIN `tabAccount` acc_parent
            ON acc_parent.name = acc.parent_account
        WHERE {where_clause}
        GROUP BY inv.name, inv.is_return
    """

    base_rows = frappe.db.sql(base_query, values, as_dict=True)
    base_map = {row.invoice: row for row in base_rows}

    if not base_map:
        return {
            "purchase": {"amount": 0, "adjustment": 0, "net_vat": 0},
            "expense": {"amount": 0, "adjustment": 0, "net_vat": 0},
            "asset": {"amount": 0, "adjustment": 0, "net_vat": 0},
        }

    # 2) Get each tax row separately (so different VAT rates / item tax template are not mixed).
    # Each row has its own taxable base = tax_amount / (rate/100). We then split that base
    # into purchase/expense/asset by this invoice's item proportion.
    vat_query = f"""
        SELECT
            inv.name AS invoice,
            inv.is_return,
            tax.account_head,
            tax.tax_amount,
            COALESCE(NULLIF(tax.rate, 0), tax_acc.tax_rate, 0) AS tax_rate
        FROM `tabPurchase Invoice` inv
        INNER JOIN `tabPurchase Taxes and Charges` tax
            ON tax.parent = inv.name
        INNER JOIN `tabAccount` tax_acc
            ON tax_acc.name = tax.account_head
        WHERE
            {where_clause}
            AND tax_acc.account_type = 'Tax'
            {account_condition}
    """

    vat_rows = frappe.db.sql(vat_query, values, as_dict=True)

    # Per-invoice zero-rated base must be computed from ALL tax rows on the invoice (not just this group),
    # otherwise a 0% group would incorrectly pick up the entire invoice base.
    all_tax_rows = frappe.db.sql(
        f"""
        SELECT
            inv.name AS invoice,
            tax.tax_amount,
            COALESCE(NULLIF(tax.rate, 0), tax_acc.tax_rate, 0) AS tax_rate
        FROM `tabPurchase Invoice` inv
        INNER JOIN `tabPurchase Taxes and Charges` tax ON tax.parent = inv.name
        INNER JOIN `tabAccount` tax_acc ON tax_acc.name = tax.account_head
        WHERE
            {where_clause}
            AND tax_acc.account_type = 'Tax'
        """,
        values,
        as_dict=True,
    )

    # Per-invoice: base already covered by non-zero tax rows; remaining = zero-rated base
    base_from_positive_rate = {}
    zero_rate_row_count = {}
    for row in all_tax_rows:
        inv = row.invoice
        tax_rate = flt(row.tax_rate, 2) or 0
        if tax_rate > 0:
            base_from_positive_rate[inv] = base_from_positive_rate.get(inv, 0) + (
                abs(flt(row.tax_amount, 2)) / (tax_rate / 100)
            )
        else:
            zero_rate_row_count[inv] = zero_rate_row_count.get(inv, 0) + 1

    # Zero-rated taxable base = invoice total base minus base at non-zero rates (split across 0% rows).
    # Use abs() so return invoices (negative total_base) are handled correctly.
    zero_rated_base_per_row = {}
    for inv, base_info in base_map.items():
        total_base = (base_info.purchase_base or 0) + (base_info.expense_base or 0) + (base_info.asset_base or 0)
        abs_total = abs(total_base)
        if not abs_total:
            continue
        covered = base_from_positive_rate.get(inv, 0)
        zero_base = max(0, abs_total - covered)
        n_zero = max(1, zero_rate_row_count.get(inv, 0))
        zero_rated_base_per_row[inv] = zero_base / n_zero  # always positive magnitude

    totals = {
        "purchase": {
            "amount": 0,  # taxable base for normal invoices
            "adjustment": 0,  # taxable base for returns
            "vat_amount": 0,  # VAT on normal invoices
            "vat_adjustment": 0,  # VAT on returns
        },
        "expense": {
            "amount": 0,
            "adjustment": 0,
            "vat_amount": 0,
            "vat_adjustment": 0,
        },
        "asset": {
            "amount": 0,
            "adjustment": 0,
            "vat_amount": 0,
            "vat_adjustment": 0,
        },
    }

    for row in vat_rows:
        base_info = base_map.get(row.invoice)
        if not base_info:
            continue

        purchase_base = base_info.purchase_base or 0
        expense_base = base_info.expense_base or 0
        asset_base = base_info.asset_base or 0

        total_base = purchase_base + expense_base + asset_base
        abs_total = abs(total_base)
        if not abs_total:
            continue

        # Taxable base for this tax row: from tax_amount/rate when rate > 0, else zero-rated base
        tax_rate = flt(row.tax_rate, 2) or 0
        if tax_rate > 0:
            row_base = abs(flt(row.tax_amount, 2)) / (tax_rate / 100)
        else:
            row_base = zero_rated_base_per_row.get(row.invoice, 0)

        net_vat = flt(row.tax_amount, 2) or 0
        if row.is_return:
            net_vat = -abs(net_vat)

        # Split by proportion using abs values so returns (negative bases) give the correct ratio
        purchase_share = row_base * (abs(purchase_base) / abs_total)
        expense_share = row_base * (abs(expense_base) / abs_total)
        asset_share = row_base * (abs(asset_base) / abs_total)

        vat_purchase = net_vat * (purchase_base / total_base)
        vat_expense = net_vat * (expense_base / total_base)
        vat_asset = net_vat * (asset_base / total_base)

        # For non-return invoices: accumulate taxable base as "amount" and VAT as "vat_amount".
        # For returns: base goes to "adjustment", VAT to "vat_adjustment".
        if row.is_return:
            totals["purchase"]["adjustment"] += abs(purchase_share)
            totals["expense"]["adjustment"] += abs(expense_share)
            totals["asset"]["adjustment"] += abs(asset_share)

            totals["purchase"]["vat_adjustment"] += abs(vat_purchase)
            totals["expense"]["vat_adjustment"] += abs(vat_expense)
            totals["asset"]["vat_adjustment"] += abs(vat_asset)
        else:
            totals["purchase"]["amount"] += purchase_share
            totals["expense"]["amount"] += expense_share
            totals["asset"]["amount"] += asset_share

            totals["purchase"]["vat_amount"] += vat_purchase
            totals["expense"]["vat_amount"] += vat_expense
            totals["asset"]["vat_amount"] += vat_asset

    # Convert internal VAT breakdown to a single net_vat field per bucket
    result = {}
    for key, value in totals.items():
        net_vat = (value.get("vat_amount", 0) or 0) - (value.get("vat_adjustment", 0) or 0)
        result[key] = {
            "amount": value.get("amount", 0) or 0,
            "adjustment": value.get("adjustment", 0) or 0,
            "net_vat": net_vat,
        }

    return result

def get_bayan_totals(filters):
    """Return amount (non-return), adjustment (return), and net for custom_bayan_value."""
    conditions = [
        "docstatus = 1",
        "posting_date BETWEEN %(from_date)s AND %(to_date)s",
        "COALESCE(custom_bayan_value, 0) != 0",
    ]
    values = dict(filters)
    if filters.get("company"):
        conditions.append("company = %(company)s")
    where = " AND ".join(conditions)
    rows = frappe.db.sql(
        f"""
        SELECT
            SUM(CASE WHEN is_return = 0 THEN custom_bayan_value ELSE 0 END) AS amount,
            SUM(CASE WHEN is_return = 1 THEN custom_bayan_value ELSE 0 END) AS adjustment
        FROM `tabPurchase Invoice`
        WHERE {where}
        """,
        values,
        as_dict=True,
    )
    amount = flt((rows or [{}])[0].get("amount"))
    adjustment = flt((rows or [{}])[0].get("adjustment"))
    return {
        "amount": amount,
        "adjustment": adjustment,
        "net_vat": amount - adjustment,
    }


def get_data(filters):
    data = []
    groups = get_account_group_map()

    # ---------- VAT ON SALES ----------
    data.append({
        "title": "<b>VAT on Sales</b>",
        "amount": None,
        "adjustment": None,
        "net_vat_amount": None
    })

    sales_total = 0
    sales_total_amount = 0
    sales_total_adjustment = 0

    for label, info in groups["Sales"].items():
        rows = get_taxable_summary(
            "tabSales Invoice",
            "tabSales Taxes and Charges",
            filters,
            info["accounts"],
            info["tax_rate"],
            is_sales=True
        )

        amount = sum(r.get("amount", 0) for r in rows)
        adjustment = sum(r.get("adjustment", 0) for r in rows)

        sales_total_amount += amount
        sales_total_adjustment += adjustment

        tax_rate = info["tax_rate"] or 0
        net_vat = (amount - adjustment) * (tax_rate / 100)

        sales_total += net_vat

        data.append({
            "title": get_detail_link(label, "Sales", info.get("group_name") or label, filters),
            "amount": amount,
            "adjustment": adjustment,
            "net_vat_amount": net_vat
        })


    data.append({
        "title": "<b>Total Sales VAT</b>",
        "amount": sales_total_amount,
        "adjustment": sales_total_adjustment,
        "net_vat_amount": sales_total
    })

    data.append({
        "title": None,
        "amount": None,
        "adjustment": None,
        "net_vat_amount": None
    })

    # ---------- VAT ON PURCHASES ----------
    data.append({
        "title": "<b>VAT on Purchases</b>",
        "amount": None,
        "adjustment": None,
        "net_vat_amount": None
    })
    purchase_split = get_purchase_vat_split(filters)

    purchase_total = 0
    purchase_total_amount = 0
    purchase_total_adjustment = 0

    purchase_labels = list(groups["Purchase"].keys())
    for label, info in groups["Purchase"].items():
        split = get_purchase_vat_split(filters, info["accounts"])

        group_amount = 0
        group_adjustment = 0
        group_vat = 0

        for key, title_suffix in (
            ("purchase", "Purchase"),
            ("expense", "Expense"),
            ("asset", "Asset Purchase"),
        ):
            bucket = split.get(key, {}) or {}
            amount = bucket.get("amount", 0) or 0
            adjustment = bucket.get("adjustment", 0) or 0
            net_vat = bucket.get("net_vat", 0) or 0

            purchase_total += net_vat
            purchase_total_amount += amount
            purchase_total_adjustment += adjustment
            group_amount += amount
            group_adjustment += adjustment
            group_vat += net_vat

            data.append({
                "title": get_detail_link(
                    f"{label} - {title_suffix}",
                    "Purchase",
                    info.get("group_name") or label,
                    filters,
                    bucket=title_suffix,
                ),
                "amount": amount,
                "adjustment": adjustment,
                "net_vat_amount": net_vat,
            })

        # Subtotal row per group (only when there are multiple groups)
        if len(purchase_labels) > 1:
            data.append({
                "title": f"<b>Total {label}</b>",
                "amount": group_amount,
                "adjustment": group_adjustment,
                "net_vat_amount": group_vat,
            })

    data.append({
        "title": "<b>Total Purchase VAT</b>",
        "amount": purchase_total_amount,
        "adjustment": purchase_total_adjustment,
        "net_vat_amount": purchase_total
    })

    data.append({
        "title": None,
        "amount": None,
        "adjustment": None,
        "net_vat_amount": None
    })

    # ---------- BAYAN ----------
    bayan = get_bayan_totals(filters)
    bayan_total = bayan["net_vat"]

    data.append({
        "title": "<b>Bayan</b>",
        "amount": None,
        "adjustment": None,
        "net_vat_amount": None
    })
    data.append({
        "title": get_detail_link("Total Bayan Value", "Bayan", None, filters),
        "amount": bayan["amount"],
        "adjustment": bayan["adjustment"],
        "net_vat_amount": 0
    })

    data.append({
        "title": None,
        "amount": None,
        "adjustment": None,
        "net_vat_amount": None
    })

    # ---------- VAT ON OTHER EXPENSES ----------
    data.append({
        "title": "<b>VAT on Other Expenses</b>",
        "amount": None,
        "adjustment": None,
        "net_vat_amount": None
    })

    expense_total = 0

    for label, info in groups["Expense"].items():
        # Get VAT from Journal Entries
        je_vat = get_expense_vat_from_journal_entries(
            filters,
            info["accounts"]
        )
        
        # Get VAT from Expense Claims (only if Expense Claim is present)
        ec_vat = 0
        if frappe.db.exists("DocType", "Expense Claim"):
            ec_vat = get_expense_vat_from_expense_claims(
                filters,
                info["accounts"]
            )
        
        # Total VAT for this expense group
        net_vat = je_vat + ec_vat

        expense_total += net_vat

        data.append({
            "title": label,
            "amount": None,
            "adjustment": None,
            "net_vat_amount": net_vat
        })

    data.append({
        "title": "<b>Total Other Expenses VAT</b>",
        "amount": None,
        "adjustment": None,
        "net_vat_amount": expense_total
    })

    data.append({
        "title": None,
        "amount": None,
        "adjustment": None,
        "net_vat_amount": None
    })

    # ---------- NET VAT ----------
    data.append({
        "title": "<b>Net VAT Due</b>",
        "amount": None,
        "adjustment": None,
        "net_vat_amount": None
    })

    net_vat_due = sales_total - (purchase_total + expense_total + bayan_total)
    net_amount_due = sales_total_amount - purchase_total_amount
    net_adjustment_due = sales_total_adjustment - purchase_total_adjustment

    data.append({
        "title": "Total VAT due for current period",
        "amount": net_amount_due,
        "adjustment": net_adjustment_due,
        "net_vat_amount": net_vat_due
    })

    # Compute net_amount = amount - adjustment for every row
    for row in data:
        amt = row.get("amount")
        adj = row.get("adjustment")
        if amt is not None and adj is not None:
            row["net_amount"] = flt(amt) - flt(adj)
        else:
            row["net_amount"] = None

    return data