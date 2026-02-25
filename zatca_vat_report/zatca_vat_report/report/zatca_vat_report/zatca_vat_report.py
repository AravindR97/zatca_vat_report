# Copyright (c) 2026, Aravind R and contributors
# For license information, please see license.txt

import frappe


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
        {"fieldname": "net_vat_amount", "label": "Net VAT Amount", "fieldtype": "Currency", "width": 150},
    ]

from collections import defaultdict


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
            "accounts": [acc.account for acc in group.linked_accounts],
            "tax_rate": row.tax_rate
        }

    # Purchase groups
    for row in settings.purchase_account_groups:
        group = frappe.get_doc("ZATCA Account Group", row.account_group)
        result["Purchase"][group.account_group_label] = {
            "accounts": [acc.account for acc in group.linked_accounts],
            "tax_rate": row.tax_rate
        }

    # Expense groups
    for row in settings.expense_account_groups:
        group = frappe.get_doc("ZATCA Account Group", row.account_group)
        result["Expense"][group.account_group_label] = {
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

    # 2) Get total VAT per invoice from tax rows
    vat_query = f"""
        SELECT
            inv.name AS invoice,
            inv.is_return,
            IFNULL(
                SUM(
                    CASE
                        WHEN inv.is_return = 1 THEN -ABS(tax.tax_amount)
                        ELSE ABS(tax.tax_amount)
                    END
                ),
                0
            ) AS net_vat
        FROM `tabPurchase Invoice` inv
        INNER JOIN `tabPurchase Taxes and Charges` tax
            ON tax.parent = inv.name
        INNER JOIN `tabAccount` tax_acc
            ON tax_acc.name = tax.account_head
        WHERE
            {where_clause}
            AND tax_acc.account_type = 'Tax'
            {account_condition}
        GROUP BY inv.name, inv.is_return
    """

    vat_rows = frappe.db.sql(vat_query, values, as_dict=True)

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
        if not total_base:
            continue

        net_vat = row.net_vat or 0

        purchase_share = net_vat * (purchase_base / total_base)
        expense_share = net_vat * (expense_base / total_base)
        asset_share = net_vat * (asset_base / total_base)

        # For non-return invoices: accumulate taxable base as "amount"
        # and VAT as "vat_amount".
        # For returns: base goes to "adjustment", VAT to "vat_adjustment".
        if row.is_return:
            totals["purchase"]["adjustment"] += abs(purchase_base)
            totals["expense"]["adjustment"] += abs(expense_base)
            totals["asset"]["adjustment"] += abs(asset_base)

            totals["purchase"]["vat_adjustment"] += abs(purchase_share)
            totals["expense"]["vat_adjustment"] += abs(expense_share)
            totals["asset"]["vat_adjustment"] += abs(asset_share)
        else:
            totals["purchase"]["amount"] += purchase_base
            totals["expense"]["amount"] += expense_base
            totals["asset"]["amount"] += asset_base

            totals["purchase"]["vat_amount"] += purchase_share
            totals["expense"]["vat_amount"] += expense_share
            totals["asset"]["vat_amount"] += asset_share

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

        tax_rate = info["tax_rate"] or 0
        net_vat = (amount - adjustment) * (tax_rate / 100)

        sales_total += net_vat

        data.append({
            "title": label,
            "amount": amount,
            "adjustment": adjustment,
            "net_vat_amount": net_vat
        })


    data.append({
        "title": "<b>Total Sales VAT</b>",
        "amount": None,
        "adjustment": None,
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

    for label, info in groups["Purchase"].items():
        split = get_purchase_vat_split(filters, info["accounts"])

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

            data.append({
                "title": f"{label} - {title_suffix}",
                "amount": amount,
                "adjustment": adjustment,
                "net_vat_amount": net_vat,
            })

    data.append({
        "title": "<b>Total Purchase VAT</b>",
        "amount": None,
        "adjustment": None,
        "net_vat_amount": purchase_total
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

    data.append({
        "title": "Total VAT due for current period",
        "net_vat_amount": sales_total - (purchase_total + expense_total)
    })

    return data