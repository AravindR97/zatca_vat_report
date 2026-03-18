// Copyright (c) 2026, Aravind R and contributors
// For license information, please see license.txt

frappe.query_reports["ZATCA VAT Report Detail"] = {
	filters: [
		{
			fieldname: "section",
			label: __("Section"),
			fieldtype: "Select",
			options: ["Purchase", "Sales", "Bayan"],
			default: "Purchase",
			reqd: 1,
		},
		{
			fieldname: "group_label",
			label: __("Account Group"),
			fieldtype: "Link",
			options: "ZATCA Account Group",
			depends_on: "eval:doc.section!=='Bayan'",
		},
		{
			fieldname: "bucket",
			label: __("Bucket"),
			fieldtype: "Select",
			options: ["Purchase", "Expense", "Asset Purchase"],
			depends_on: "eval:doc.section==='Purchase'",
		},
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.month_start(),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.month_end(),
			reqd: 1,
		},
	],
};
