// Copyright (c) 2026, Aravind R and contributors
// For license information, please see license.txt

frappe.query_reports["ZATCA VAT Report"] = {
	"filters": [
		{
			"fieldname": "from_date",
			"label": "From Date",
			"fieldtype": "Date",
			"width": 100,
			"reqiured": 1
		},
		{
			"fieldname": "to_date",
			"label": "To Date",
			"fieldtype": "Date",
			"width": 100,
			"reqiured": 1
		},
	]
};
