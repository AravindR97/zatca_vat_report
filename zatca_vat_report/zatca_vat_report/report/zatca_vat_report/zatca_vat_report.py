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

    if not is_sales:
        conditions.append("(inv.bill_date IS NULL OR inv.bill_date >= %(from_date)s)")

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
                                THEN ABS(tax.tax_amount) / (acc_master.tax_rate / 100)
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
                                THEN ABS(tax.tax_amount) / (acc_master.tax_rate / 100)
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



def get_data(filters):
    data = []
    groups = get_account_group_map()

    # ---------- VAT ON SALES ----------
    data.append({"title": "<b>VAT on Sales</b>"})

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
        "net_vat_amount": sales_total
    })

    data.append({})

    # ---------- VAT ON PURCHASES ----------
    data.append({"title": "<b>VAT on Purchases</b>"})

    purchase_total = 0

    for label, info in groups["Purchase"].items():
        rows = get_taxable_summary(
            "tabPurchase Invoice",
            "tabPurchase Taxes and Charges",
            filters,
            info["accounts"],
            info["tax_rate"],
            is_sales=False
        )

        amount = sum(r.get("amount", 0) for r in rows)
        adjustment = sum(r.get("adjustment", 0) for r in rows)

        tax_rate = info["tax_rate"] or 0
        net_vat = (amount - adjustment) * (tax_rate / 100)

        purchase_total += net_vat

        data.append({
            "title": label,
            "amount": amount,
            "adjustment": adjustment,
            "net_vat_amount": net_vat
        })


    data.append({
        "title": "<b>Total Purchase VAT</b>",
        "net_vat_amount": purchase_total
    })

    data.append({})

    # ---------- VAT ON OTHER EXPENSES ----------
    data.append({"title": "<b>VAT on Other Expenses</b>"})

    expense_total = 0

    for label, info in groups["Expense"].items():
        net_vat = get_expense_vat_from_journal_entries(
            filters,
            info["accounts"]
        )

        expense_total += net_vat

        data.append({
            "title": label,
            "net_vat_amount": net_vat
        })

    data.append({
        "title": "<b>Total Other Expenses VAT</b>",
        "net_vat_amount": expense_total
    })

    data.append({})

    # ---------- NET VAT ----------
    data.append({"title": "<b>Net VAT Due</b>"})

    data.append({
        "title": "Total VAT due for current period",
        "net_vat_amount": sales_total - (purchase_total + expense_total)
    })

    return data