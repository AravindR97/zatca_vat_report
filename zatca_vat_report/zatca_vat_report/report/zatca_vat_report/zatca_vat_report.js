// Copyright (c) 2026, Aravind R and contributors
// For license information, please see license.txt

frappe.query_reports["ZATCA VAT Report"] = {
	"filters": [
		{
			"fieldname": "company",
			"label": "Company",
			"fieldtype": "Link",
			"options": "Company",
			"reqd": 0,
			"description": "Leave blank to show all companies combined"
		},
		{
			"fieldname": "from_date",
			"label": "From Date",
			"fieldtype": "Date",
			"width": 100,
			"reqd": 1
		},
		{
			"fieldname": "to_date",
			"label": "To Date",
			"fieldtype": "Date",
			"width": 100,
			"reqd": 1
		},
	],
};
