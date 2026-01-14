# Copyright (c) 2026, Aravind R and contributors
# For license information, please see license.txt

import frappe


def execute(filters=None):
    if not filters:
        filters = {}

    if not filters.get("from_date") or not filters.get("to_date"):
        frappe.throw("From Date and To Date are mandatory")

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

    return result


def get_taxable_summary(doctype, tax_table, filters, accounts, tax_rate, is_sales=True):
    conditions = []
    values = {}

    if filters.get("company"):
        conditions.append("inv.company = %(company)s")
        values["company"] = filters["company"]

    conditions.append("inv.docstatus = 1")
    conditions.append("inv.posting_date BETWEEN %(from_date)s AND %(to_date)s")

    if not is_sales:
        conditions.append("(inv.bill_date IS NULL OR inv.bill_date >= %(from_date)s)")

    values.update(filters)

    account_condition = ""
    if accounts:
        account_condition = "AND tax.account_head IN %(accounts)s"
        values["accounts"] = tuple(accounts)

    # ---------------- ZERO RATED LOGIC ----------------
    if tax_rate == 0:
        query = f"""
            SELECT
                tax.account_head,
                IFNULL(
                    SUM(
                        CASE
                            WHEN inv.is_return = 0 THEN inv.base_net_total
                            ELSE 0
                        END
                    ), 0
                ) AS amount,

                IFNULL(
                    SUM(
                        CASE
                            WHEN inv.is_return = 1 THEN ABS(inv.base_net_total)
                            ELSE 0
                        END
                    ), 0
                ) AS adjustment
            FROM `{tax_table}` tax
            INNER JOIN `{doctype}` inv ON tax.parent = inv.name
            INNER JOIN `tabAccount` acc ON tax.account_head = acc.name
            WHERE
                acc.account_type = 'Tax'
                {account_condition}
                AND {' AND '.join(conditions)}
            GROUP BY tax.account_head
        """
        return frappe.db.sql(query, values, as_dict=True)

    # ---------------- STANDARD / OTHER RATES ----------------
    query = f"""
        SELECT
            tax.account_head,

            IFNULL(
                SUM(
                    CASE
                        WHEN inv.is_return = 0
                             AND total_tax.total_tax_amount > 0 THEN
                            (ABS(tax.tax_amount) / total_tax.total_tax_amount)
                            * inv.base_net_total
                        ELSE 0
                    END
                ), 0
            ) AS amount,

            IFNULL(
                SUM(
                    CASE
                        WHEN inv.is_return = 1
                             AND total_tax.total_tax_amount > 0 THEN
                            (ABS(tax.tax_amount) / total_tax.total_tax_amount)
                            * ABS(inv.base_net_total)
                        ELSE 0
                    END
                ), 0
            ) AS adjustment

        FROM `{tax_table}` tax
        INNER JOIN `{doctype}` inv ON tax.parent = inv.name
        INNER JOIN (
            SELECT parent, SUM(ABS(tax_amount)) AS total_tax_amount
            FROM `{tax_table}`
            GROUP BY parent
        ) total_tax ON total_tax.parent = tax.parent
        INNER JOIN `tabAccount` acc ON tax.account_head = acc.name
        WHERE
            acc.account_type = 'Tax'
            {account_condition}
            AND {' AND '.join(conditions)}
        GROUP BY tax.account_head
    """

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

        amount = sum(r.amount for r in rows)
        adjustment = sum(r.adjustment for r in rows)

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

        amount = sum(r.amount for r in rows)
        adjustment = sum(r.adjustment for r in rows)

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

    # ---------- NET VAT ----------
    data.append({"title": "<b>Net VAT Due</b>"})

    data.append({
        "title": "Total VAT due for current period",
        "net_vat_amount": sales_total - purchase_total
    })

    return data
