# Copyright (c) 2026, Aravind R and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ZATCAAccountGroup(Document):
	def validate(self):
		tax_rate_sum = 0.0
		if not self.linked_accounts:
			frappe.throw("At least one linked account is required in the account group.")
		for row in self.linked_accounts:
			tax_rate_sum += row.account_tax_rate

		avg_tax_rate = tax_rate_sum / len(self.linked_accounts)
		self.tax_rate = avg_tax_rate