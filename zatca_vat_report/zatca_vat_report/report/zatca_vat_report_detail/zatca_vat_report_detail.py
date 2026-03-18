# Copyright (c) 2026, Aravind R and contributors
# For license information, please see license.txt

import frappe
from frappe.utils import flt, getdate


def execute(filters=None):
	if not filters:
		filters = {}

	section = (filters.get("section") or "Purchase").strip()
	group_label = (filters.get("group_label") or "").strip()
	if not group_label:
		return _get_columns(section), []

	if not filters.get("from_date") or not filters.get("to_date"):
		return _get_columns(section), []

	from_date = getdate(filters.get("from_date"))
	to_date = getdate(filters.get("to_date"))
	company = (filters.get("company") or "").strip()

	group = frappe.get_doc("ZATCA Account Group", group_label)
	accounts = [r.account for r in (group.get("linked_accounts") or []) if r.account]

	if section == "Sales":
		columns = _get_columns("Sales")
		data = _get_sales_detail(from_date, to_date, company, accounts)
		return columns, data

	# Purchase
	bucket = (filters.get("bucket") or "Purchase").strip()
	columns = _get_columns("Purchase")
	data = _get_purchase_detail(from_date, to_date, company, accounts, bucket)
	return columns, data


def _get_columns(section: str):
	if section == "Sales":
		return [
			{"fieldname": "invoice", "label": "Sales Invoice", "fieldtype": "Link", "options": "Sales Invoice", "width": 140},
			{"fieldname": "posting_date", "label": "Posting Date", "fieldtype": "Date", "width": 110},
			{"fieldname": "customer_name", "label": "Customer", "fieldtype": "Data", "width": 200},
			{"fieldname": "customer", "label": "Customer ID", "fieldtype": "Link", "options": "Customer", "width": 150},
			{"fieldname": "custom_vat_registration_number", "label": "VAT Reg. No.", "fieldtype": "Data", "width": 150},
			{"fieldname": "base_amount", "label": "Taxable Base", "fieldtype": "Currency", "width": 140},
			{"fieldname": "vat_amount", "label": "VAT", "fieldtype": "Currency", "width": 120},
			{"fieldname": "grand_total", "label": "Grand Total", "fieldtype": "Currency", "width": 130},
			{"fieldname": "is_return", "label": "Is Return", "fieldtype": "Check", "width": 90},
		]

	return [
		{"fieldname": "invoice", "label": "Purchase Invoice", "fieldtype": "Link", "options": "Purchase Invoice", "width": 140},
		{"fieldname": "posting_date", "label": "Posting Date", "fieldtype": "Date", "width": 110},
		{"fieldname": "supplier_name", "label": "Supplier", "fieldtype": "Data", "width": 200},
		{"fieldname": "tax_id", "label": "Tax ID", "fieldtype": "Data", "width": 150},
		{"fieldname": "bucket", "label": "Bucket", "fieldtype": "Data", "width": 140},
		{"fieldname": "base_amount", "label": "Taxable Base", "fieldtype": "Currency", "width": 140},
		{"fieldname": "vat_amount", "label": "VAT", "fieldtype": "Currency", "width": 120},
		{"fieldname": "grand_total", "label": "Grand Total", "fieldtype": "Currency", "width": 130},
		{"fieldname": "is_return", "label": "Is Return", "fieldtype": "Check", "width": 90},
	]


def _base_conditions(from_date, to_date, company, alias):
	conds = [f"{alias}.docstatus = 1", f"{alias}.posting_date BETWEEN %(from_date)s AND %(to_date)s"]
	values = {"from_date": from_date, "to_date": to_date}
	if company:
		conds.append(f"{alias}.company = %(company)s")
		values["company"] = company
	return " AND ".join(conds), values


def _get_sales_detail(from_date, to_date, company, tax_accounts):
	if not tax_accounts:
		return []

	where_clause, values = _base_conditions(from_date, to_date, company, "si")
	values["accounts"] = tuple(tax_accounts)

	# Pull selected tax rows (so multiple VAT rates are not mixed)
	rows = frappe.db.sql(
		f"""
		SELECT
			si.name AS invoice,
			si.posting_date,
			si.customer,
			si.customer_name,
			cust.custom_vat_registration_number,
			si.is_return,
			COALESCE(stc.base_tax_amount_after_discount_amount, stc.base_tax_amount) AS base_tax_amount,
			COALESCE(NULLIF(stc.rate, 0), acc.tax_rate, 0) AS tax_rate
		FROM `tabSales Invoice` si
		INNER JOIN `tabSales Taxes and Charges` stc ON stc.parent = si.name
		INNER JOIN `tabAccount` acc ON acc.name = stc.account_head
		LEFT JOIN `tabCustomer` cust ON cust.name = si.customer
		WHERE
			{where_clause}
			AND acc.account_type = 'Tax'
			AND stc.account_head IN %(accounts)s
		""",
		values,
		as_dict=True,
	)

	# Compute per-invoice: positive-rate base and zero-rate base distribution from ALL tax rows
	all_rows = frappe.db.sql(
		f"""
		SELECT
			si.name AS invoice,
			COALESCE(stc.base_tax_amount_after_discount_amount, stc.base_tax_amount) AS base_tax_amount,
			COALESCE(NULLIF(stc.rate, 0), acc.tax_rate, 0) AS tax_rate
		FROM `tabSales Invoice` si
		INNER JOIN `tabSales Taxes and Charges` stc ON stc.parent = si.name
		INNER JOIN `tabAccount` acc ON acc.name = stc.account_head
		WHERE
			{where_clause}
			AND acc.account_type = 'Tax'
		""",
		values,
		as_dict=True,
	)

	base_from_positive_rate = {}
	zero_rate_row_count = {}
	for r in all_rows:
		inv = r.invoice
		rate = flt(r.tax_rate) or 0
		if rate > 0:
			base_from_positive_rate[inv] = base_from_positive_rate.get(inv, 0) + (
				abs(flt(r.base_tax_amount)) / (rate / 100)
			)
		else:
			zero_rate_row_count[inv] = zero_rate_row_count.get(inv, 0) + 1

	# invoice total base (use base_net_total)
	invoice_base = {}
	base_rows = frappe.db.sql(
		f"""
		SELECT si.name AS invoice, si.base_net_total
		FROM `tabSales Invoice` si
		WHERE {where_clause}
		""",
		values,
		as_dict=True,
	)
	for r in base_rows:
		invoice_base[r.invoice] = flt(r.base_net_total)

	zero_base_per_row = {}
	for inv, total_base in invoice_base.items():
		covered = base_from_positive_rate.get(inv, 0)
		zero_base = max(0, flt(total_base) - covered)
		n_zero = max(1, zero_rate_row_count.get(inv, 0))
		zero_base_per_row[inv] = zero_base / n_zero

	# Aggregate per invoice
	out_map = {}
	for r in rows:
		inv = r.invoice
		rate = flt(r.tax_rate) or 0
		if rate > 0:
			row_base = abs(flt(r.base_tax_amount)) / (rate / 100)
		else:
			row_base = zero_base_per_row.get(inv, 0)

		# Returns: negate both base and VAT
		if r.is_return:
			row_base = -abs(row_base)

		vat = flt(r.base_tax_amount) or 0
		if r.is_return:
			vat = -abs(vat)

		rec = out_map.setdefault(
			inv,
			{
				"invoice": inv,
				"posting_date": r.posting_date,
				"customer_name": r.customer_name or r.customer,
				"customer": r.customer,
				"custom_vat_registration_number": r.custom_vat_registration_number or "",
				"base_amount": 0,
				"vat_amount": 0,
				"is_return": r.is_return,
			},
		)
		rec["base_amount"] += row_base
		rec["vat_amount"] += vat

	result = list(out_map.values())
	for rec in result:
		rec["grand_total"] = flt(rec["base_amount"]) + flt(rec["vat_amount"])
	result.sort(key=lambda x: (x.get("posting_date") or "", x.get("invoice") or ""))
	return result


def _classify_bucket(base_info, bucket_name):
	# base_info has purchase_base/expense_base/asset_base
	if bucket_name == "Expense":
		return flt(base_info.get("expense_base"))
	if bucket_name == "Asset Purchase":
		return flt(base_info.get("asset_base"))
	return flt(base_info.get("purchase_base"))


def _get_purchase_bucket_base_map(from_date, to_date, company):
	where_clause, values = _base_conditions(from_date, to_date, company, "pi")
	# Keep logic consistent with main report (account_type with parent fallback)
	base_rows = frappe.db.sql(
		f"""
		SELECT
			pi.name AS invoice,
			pi.posting_date,
			pi.supplier,
			pi.supplier_name,
			sup.tax_id,
			pi.is_return,
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
		FROM `tabPurchase Invoice` pi
		INNER JOIN `tabPurchase Invoice Item` pii ON pii.parent = pi.name
		LEFT JOIN `tabAccount` acc ON acc.name = pii.expense_account
		LEFT JOIN `tabAccount` acc_parent ON acc_parent.name = acc.parent_account
		LEFT JOIN `tabSupplier` sup ON sup.name = pi.supplier
		WHERE {where_clause}
		GROUP BY pi.name, pi.posting_date, pi.supplier, pi.supplier_name, sup.tax_id, pi.is_return
		""",
		values,
		as_dict=True,
	)

	out = {}
	for r in base_rows:
		out[r.invoice] = r
	return out, values


def _get_purchase_detail(from_date, to_date, company, tax_accounts, bucket):
	if not tax_accounts:
		return []

	base_map, values = _get_purchase_bucket_base_map(from_date, to_date, company)
	values["accounts"] = tuple(tax_accounts)

	where_clause, _ = _base_conditions(from_date, to_date, company, "pi")

	# Pull selected tax rows for invoices in this period and tax accounts
	tax_rows = frappe.db.sql(
		f"""
		SELECT
			pi.name AS invoice,
			pi.is_return,
			COALESCE(ptc.base_tax_amount_after_discount_amount, ptc.base_tax_amount) AS base_tax_amount,
			COALESCE(NULLIF(ptc.rate, 0), acc.tax_rate, 0) AS tax_rate
		FROM `tabPurchase Invoice` pi
		INNER JOIN `tabPurchase Taxes and Charges` ptc ON ptc.parent = pi.name
		INNER JOIN `tabAccount` acc ON acc.name = ptc.account_head
		WHERE
			{where_clause}
			AND acc.account_type = 'Tax'
			AND ptc.account_head IN %(accounts)s
		""",
		values,
		as_dict=True,
	)

	# Per-invoice: base already covered by positive-rate rows; remaining = zero-rated base.
	# Must be computed from ALL tax rows on the invoice (not just selected accounts).
	all_tax_rows = frappe.db.sql(
		f"""
		SELECT
			pi.name AS invoice,
			COALESCE(ptc.base_tax_amount_after_discount_amount, ptc.base_tax_amount) AS base_tax_amount,
			COALESCE(NULLIF(ptc.rate, 0), acc.tax_rate, 0) AS tax_rate
		FROM `tabPurchase Invoice` pi
		INNER JOIN `tabPurchase Taxes and Charges` ptc ON ptc.parent = pi.name
		INNER JOIN `tabAccount` acc ON acc.name = ptc.account_head
		WHERE
			{where_clause}
			AND acc.account_type = 'Tax'
		""",
		values,
		as_dict=True,
	)

	base_from_positive_rate = {}
	zero_rate_row_count = {}
	for r in all_tax_rows:
		inv = r.invoice
		rate = flt(r.tax_rate) or 0
		if rate > 0:
			base_from_positive_rate[inv] = base_from_positive_rate.get(inv, 0) + (
				abs(flt(r.base_tax_amount)) / (rate / 100)
			)
		else:
			zero_rate_row_count[inv] = zero_rate_row_count.get(inv, 0) + 1

	zero_base_per_row = {}
	for inv, base_info in base_map.items():
		total_base = flt(base_info.purchase_base) + flt(base_info.expense_base) + flt(base_info.asset_base)
		abs_total = abs(total_base)
		if not abs_total:
			continue
		covered = base_from_positive_rate.get(inv, 0)
		zero_base = max(0, abs_total - covered)
		n_zero = max(1, zero_rate_row_count.get(inv, 0))
		zero_base_per_row[inv] = zero_base / n_zero  # always positive magnitude

	# Aggregate per invoice for requested bucket only
	out_map = {}
	for r in tax_rows:
		base_info = base_map.get(r.invoice)
		if not base_info:
			continue

		total_base = flt(base_info.purchase_base) + flt(base_info.expense_base) + flt(base_info.asset_base)
		abs_total = abs(total_base)
		if not abs_total:
			continue

		bucket_base = _classify_bucket(base_info, bucket)
		abs_bucket = abs(bucket_base)
		# skip if this invoice has nothing in the requested bucket
		if not abs_bucket:
			continue

		# Use absolute values for ratio so returns are handled correctly
		ratio = abs_bucket / abs_total

		rate = flt(r.tax_rate) or 0
		if rate > 0:
			row_base = abs(flt(r.base_tax_amount)) / (rate / 100)
		else:
			row_base = zero_base_per_row.get(r.invoice, 0)

		base_share = row_base * ratio
		vat_share = abs(flt(r.base_tax_amount)) * ratio

		# Returns: negate both base and VAT
		if r.is_return:
			base_share = -base_share
			vat_share = -vat_share

		rec = out_map.setdefault(
			r.invoice,
			{
				"invoice": r.invoice,
				"posting_date": base_info.posting_date,
				"supplier_name": base_info.supplier_name or base_info.supplier,
				"tax_id": base_info.tax_id or "",
				"bucket": bucket,
				"base_amount": 0,
				"vat_amount": 0,
				"is_return": base_info.is_return,
			},
		)
		rec["base_amount"] += base_share
		rec["vat_amount"] += vat_share

	out = list(out_map.values())
	for rec in out:
		rec["grand_total"] = flt(rec["base_amount"]) + flt(rec["vat_amount"])
	out.sort(key=lambda x: (x.get("posting_date") or "", x.get("invoice") or ""))
	return out

