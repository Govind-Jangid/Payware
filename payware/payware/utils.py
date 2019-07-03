from __future__ import unicode_literals
import json
import frappe
from frappe.model.document import Document
from dateutil.relativedelta import relativedelta
from frappe.utils import cint, format_datetime,get_datetime_str,now_datetime,add_days,today,formatdate,date_diff,getdate,add_months,flt, nowdate, fmt_money, add_to_date, DATE_FORMAT, rounded
from frappe import _
from erpnext.accounts.utils import get_fiscal_year
from erpnext.hr.doctype.employee.employee import get_holiday_list_for_employee

@frappe.whitelist()
def create_disbursement_journal_entry(doc, method):
	#frappe.msgprint("Method fired: " + method)
	precision = frappe.get_precision("Journal Entry Account", "debit_in_account_currency")

	journal_entry = frappe.new_doc('Journal Entry')
	journal_entry.voucher_type = 'Bank Entry'
	journal_entry.user_remark = _('Payment of {0} disbursed on {1} starting from {2}')\
		.format(doc.name, doc.disbursement_date, doc.repayment_start_date)
	journal_entry.company = doc.company
	journal_entry.posting_date = doc.disbursement_date

	payment_amount = flt(doc.loan_amount, precision)

	journal_entry.set("accounts", [
		{
			"account": doc.loan_account,
			"credit_in_account_currency": payment_amount,
			"reference_type": doc.doctype,
			"reference_name": doc.name
		},
		{
			"account": doc.payment_account,
			"debit_in_account_currency": payment_amount,
			"reference_type": doc.doctype,
			"reference_name": doc.name
		}
	])
	journal_entry.save(ignore_permissions = True)
	frappe.msgprint("Disbursement Journal: " + journal_entry.name + " has been created.")

@frappe.whitelist()
def set_loan_paid(doc, method):
	#frappe.msgprint("Method fired: " + str(method) + " on doc: " + str(doc))
	if method == "on_submit":
		for loan in doc.loans:
			#frappe.msgprint("Loan to be updated as paid: " + str(loan.loan))
			loan = frappe.get_doc("Loan", loan.loan)
			#frappe.msgprint("Loan Doc loaded: " + str(loan.name))
			for loan_repayment_schedule in loan.repayment_schedule:
				#frappe.msgprint("Loan Repayment Date " + str(loan_repayment_schedule.payment_date))
				if getdate(doc.start_date) <= getdate(loan_repayment_schedule.payment_date) <= getdate(doc.end_date):
					frappe.set_value("Repayment Schedule", loan_repayment_schedule.name, "paid", 1)
					#frappe.msgprint("Repayment Schedule of date " + str(loan_repayment_schedule.name) + " updated.")
	elif method == "on_cancel":
		for loan in doc.loans:
			#frappe.msgprint("Loan to be updated as paid: " + str(loan.loan))
			loan = frappe.get_doc("Loan", loan.loan)
			#frappe.msgprint("Loan Doc loaded: " + str(loan.name))
			for loan_repayment_schedule in loan.repayment_schedule:
				#frappe.msgprint("Loan Repayment Date " + str(loan_repayment_schedule.payment_date))
				if getdate(doc.start_date) <= getdate(loan_repayment_schedule.payment_date) <= getdate(doc.end_date):
					#frappe.msgprint("Repayment Schedule of date " + str(loan_repayment_schedule.name) + " updated.")
					frappe.set_value("Repayment Schedule", loan_repayment_schedule.name, "paid", 0)

@frappe.whitelist()
def create_loan_repayment_jv(doc, method):
	loan = frappe.get_doc("Loan", doc.loan)
	if method == "on_submit":
		cr_account = loan.payment_account
		dr_account = loan.loan_account
	elif method == "on_cancel":
		cr_account = loan.loan_account
		dr_account = loan.payment_account
	else:
		frappe.msgprint("Unknown method on create_loan_repayment_jv")
		return
	#frappe.msgprint("Method fired: " + method)
	precision = frappe.get_precision("Journal Entry Account", "debit_in_account_currency")

	journal_entry = frappe.new_doc('Journal Entry')
	journal_entry.voucher_type = 'Bank Entry'
	journal_entry.user_remark = _('{0} - {1} on {2}').format(doc.doctype, doc.name, doc.payment_date)
	journal_entry.company = doc.company
	journal_entry.posting_date = doc.payment_date

	payment_amount = flt(doc.payment_amount, precision)

	journal_entry.set("accounts", [
		{
			"account": dr_account,
			"debit_in_account_currency": payment_amount,
			"reference_type": loan.doctype,
			"reference_name": loan.name
		},
		{
			"account": cr_account,
			"credit_in_account_currency": payment_amount,
			"reference_type": loan.doctype,
			"reference_name": loan.name
		}
	])
	journal_entry.save(ignore_permissions = True)


	# Create records in NFS child doctype of Loan doctype
	if method == "on_submit":
		#frappe.msgprint("loan nfs repayment appending...")
		loan_nfs_row = loan.append("loan_repayments_not_from_salary")
		loan_nfs_row.nfs_loan_repayment = doc.name
		loan_nfs_row.company = doc.company
		loan_nfs_row.payment_date = doc.payment_date
		loan_nfs_row.payment_amount = doc.payment_amount
		loan.save()
	elif method == "on_cancel":
		# Delete record from loan related to this repayment
		for repayment in frappe.get_all("Loan NFS Repayments", "name", {"nfs_loan_repayment": doc.name}):
			#frappe.msgprint("doc.name: " + str(repayment.name) + " for loan repayment: " + doc.name)
			frappe.db.sql("""update `tabLoan NFS Repayments` set docstatus = 0 where name = %s""", repayment.name)
			frappe.delete_doc("Loan NFS Repayments", repayment.name)

	# Update loan of Repayment Schedule child doctype of Loan doctype and set the balances right as per date
	redo_repayment_schedule(loan.name)
	set_repayment_period(loan.name)
	calculate_totals(loan.name)

	if method == "on_submit":
		frappe.set_value(doc.doctype, doc.name, "journal_name", journal_entry.name)
		msg_to_print = doc.doctype + " journal " + journal_entry.name + " has been created."
	elif method == "on_cancel":
		msg_to_print = doc.doctype + " reverse journal " + journal_entry.name + " has been created."
	frappe.msgprint(msg_to_print)

@frappe.whitelist()
def redo_repayment_schedule(loan_name):
	#frappe.msgprint("This is the parameter passed: " + str(loan_name))
	loan = frappe.get_doc("Loan", str(loan_name))
	#frappe.msgprint("This is the loan object: " + str(loan.name))
	#frappe.msgprint("This is the repayment schedule length: " + str(len(loan.repayment_schedule)))

	if (loan.docstatus != 1):
		frappe.throw("The loan is not submitted. Please Submit the loan and try again.")

	# Identify pending schedule and remove those lines from schedule
	repayment_schedule_list = frappe.get_all("Repayment Schedule", fields=["name", "parent", "paid", "change_amount", "changed_principal_amount", "changed_interest_amount", "total_payment", "payment_date"], filters={"parent": loan.name})

	payment_date = loan.repayment_start_date
	row_balance_amount = loan.loan_amount
	# Delete all non-paid and not-change_amount records from schedule
	for repayment_schedule in repayment_schedule_list:
		# frappe.msgprint(str(repayment_schedule.payment_date) + " change_amount status = " + str(repayment_schedule.change_amount) + " amount " + str(repayment_schedule.principal_amount))
		if (not repayment_schedule.paid and not repayment_schedule.change_amount):
			frappe.db.sql("""update `tabRepayment Schedule` set docstatus = 0 where name = %s""", repayment_schedule.name)
			# frappe.msgprint("Repayment schedule being cleared for record: " + str(repayment_schedule.name))
			frappe.delete_doc("Repayment Schedule", repayment_schedule.name)

	# Reload the loan doc after deleting records fur futher use
	loan = frappe.get_doc("Loan", str(loan_name))

	# Find total of repayments made
	#frappe.msgprint("Finding total of repayemnts made")
	total_repayments_made = 0
	paid_repayment_schedule_list = frappe.get_all("Repayment Schedule", fields=["name", "parent", "total_payment", "payment_date"], filters={"parent": loan.name, "paid": 1})
	for repayment_schedule in paid_repayment_schedule_list:
		total_repayments_made += repayment_schedule.total_payment

	# Find total of NFS repayments made
	#frappe.msgprint("Finding total repayemnts made")
	total_nfs_repayments_made = 0
	nfs_repayment_schedule_list = frappe.get_all("Loan NFS Repayments", fields=["parent", "payment_amount"], filters={"parent": loan.name, "docstatus": 1})
	for nfs_repayment_schedule in nfs_repayment_schedule_list:
		total_nfs_repayments_made += nfs_repayment_schedule.payment_amount

	# Find new balance_amount
	balance_amount = loan.loan_amount - (total_repayments_made + total_nfs_repayments_made)
	frappe.msgprint("Repayments records balance: " + str(balance_amount) + " with total repayments = "
		+ str(total_repayments_made) + " and total nfs repayments = " + str(total_nfs_repayments_made)
		+ " on loan of " + str(loan.loan_amount))

	repayment_schedule_list = frappe.get_list("Repayment Schedule", fields=["name", "parent", "total_payment", "payment_date", "change_amount", "changed_principal_amount", "changed_interest_amount", "balance_loan_amount"], filters={"parent": loan.name})
	repayment_schedule_list = sorted(repayment_schedule_list, key=lambda k: k['payment_date'])

	next_payment_date = loan.repayment_start_date
	# frappe.msgprint("The number of existing records in the repayment scheudule is " + str(len(repayment_schedule_list)))
	idx = 1
	for repayment_schedule in repayment_schedule_list:
		# if the dates don't match then there is a gap to fill in
		# frappe.msgprint("payment date " + str(repayment_schedule.payment_date) + " " + str(next_payment_date))
		if repayment_schedule.payment_date != next_payment_date:
			# Gap fillers
			while (repayment_schedule.payment_date != next_payment_date and balance_amount > 0):

				# frappe.msgprint("Will be creating records for " + str(next_payment_date))
				# continue
				#frappe.msgprint("Creating repayments records with balance: " + str(balance_amount))
				interest_amount = rounded(balance_amount * flt(loan.rate_of_interest) / (12 * 100))
				principal_amount = loan.monthly_repayment_amount - interest_amount
				balance_amount = balance_amount + interest_amount - loan.monthly_repayment_amount

				if balance_amount < 0:
					principal_amount += balance_amount
					balance_amount = 0.0

				total_payment = principal_amount + interest_amount
				# frappe.msgprint("Gap filler payment now is " + str(total_payment) + " and balance amount is " + str(balance_amount) + " for payment date " + str(next_payment_date))

				loan_repay_row = loan.append("repayment_schedule")
				loan_repay_row.idx = idx
				loan_repay_row.payment_date = next_payment_date
				loan_repay_row.principal_amount = principal_amount
				loan_repay_row.interest_amount = interest_amount
				loan_repay_row.total_payment = total_payment
				loan_repay_row.balance_loan_amount = balance_amount
				loan_repay_row.docstatus = 1

				next_payment_date = add_months(next_payment_date, 1)
				idx += 1

			# Since gap filling is done, save the loan doc and reload it.
			loan.save()
			loan = frappe.get_doc("Loan", str(loan_name))

		if repayment_schedule.change_amount:
			total_payment = repayment_schedule.changed_principal_amount + repayment_schedule.changed_interest_amount
			# frappe.msgprint("Setting values for changed amounts for " + str(repayment_schedule.payment_date) + " " + str(repayment_schedule.changed_principal_amount))
			frappe.set_value("Repayment Schedule", repayment_schedule.name, "idx", idx)
			frappe.set_value("Repayment Schedule", repayment_schedule.name, "principal_amount", repayment_schedule.changed_principal_amount)
			frappe.set_value("Repayment Schedule", repayment_schedule.name, "interest_amount", repayment_schedule.changed_interest_amount)
			frappe.set_value("Repayment Schedule", repayment_schedule.name, "total_payment", total_payment)
			frappe.set_value("Repayment Schedule", repayment_schedule.name, "balance_loan_amount", (balance_amount - total_payment))
		idx += 1
		# Reload loan doc again after setting values
		loan = frappe.get_doc("Loan", str(loan_name))

		# Now the repayment_schedule.payment_date == next_payment_date meaning gaps have been filled
		balance_amount -= total_payment
		next_payment_date = add_months(next_payment_date, 1)

	# End fillers. Insert rows starting balance_amount till it is 0
	while(balance_amount > 0):
		#frappe.msgprint("Creating repayments records with balance: " + str(balance_amount))
		interest_amount = rounded(balance_amount * flt(loan.rate_of_interest) / (12 * 100))
		principal_amount = loan.monthly_repayment_amount - interest_amount
		balance_amount = rounded(balance_amount + interest_amount - loan.monthly_repayment_amount)

		if balance_amount < 0:
			principal_amount += balance_amount
			balance_amount = 0.0

		total_payment = principal_amount + interest_amount
		# frappe.msgprint("Ending filler payment now is " + str(total_payment) + " and balance amount is " + str(balance_amount) + " for payment date " + str(next_payment_date))

		loan_repay_row = loan.append("repayment_schedule")
		# Find out payment date that is next to be paid.
		loan_repay_row.idx = idx
		loan_repay_row.payment_date = next_payment_date
		loan_repay_row.principal_amount = principal_amount
		loan_repay_row.interest_amount = interest_amount
		loan_repay_row.total_payment = total_payment
		loan_repay_row.balance_loan_amount = balance_amount
		loan_repay_row.docstatus = 1

		next_payment_date = add_months(next_payment_date, 1)
		idx += 1

	loan.save()

	frappe.msgprint("Loan repayment schedule redone. Created " + str(idx - 1) + " records!")

@frappe.whitelist()
def set_repayment_period(loan_docname):
	loan = frappe.get_doc("Loan", str(loan_docname))
	if loan.repayment_method == "Repay Fixed Amount per Period":
		# No need to filter out the schedules that are marked as change_amount. Show it as a period that was part of loan repayment
		loan.repayment_periods = len(loan.repayment_schedule)
	loan.save()

@frappe.whitelist()
def calculate_totals(loan_docname):
	loan = frappe.get_doc("Loan", loan_docname)
	loan.total_payment = 0
	loan.total_interest_payable = 0
	loan.total_amount_paid = 0
	for data in loan.repayment_schedule:
		loan.total_payment += data.total_payment
		loan.total_interest_payable +=data.interest_amount
		if data.paid:
			loan.total_amount_paid += data.total_payment
	loan.save()

@frappe.whitelist()
def create_additional_salary_journal(doc, method):
	#frappe.msgprint("Method fired is: " + str(method))
	if (frappe.get_value("Salary Component", doc.salary_component, "create_cash_journal")):
		salary_component = frappe.get_doc("Salary Component", doc.salary_component)
		cash_account = frappe.db.get_single_value("Payware Settings", "default_account_for_additional_component_cash_journal")
		component_account = frappe.db.get_value("Salary Component Account", {"parent": doc.salary_component, "company": doc.company}, "default_account")
		# frappe.msgprint("Expense account is: " + str(component_account))
		if method == "on_submit":
			dr_account = component_account
			cr_account = cash_account
		elif method == "on_cancel":
			dr_account = cash_account
			cr_account = component_account
		else:
			frappe.msgprint("Unknown method on create_additional_salary_journal")
			return

		#frappe.msgprint("Method fired: " + method)
		precision = frappe.get_precision("Journal Entry Account", "debit_in_account_currency")
		journal_entry = frappe.new_doc('Journal Entry')
		journal_entry.voucher_type = 'Cash Entry'
		journal_entry.user_remark = _('{2} by {1} for {3}').format(doc.doctype, doc.name, doc.salary_component, doc.employee_name)
		journal_entry.company = doc.company
		journal_entry.posting_date = doc.payroll_date

		payment_amount = flt(doc.amount, precision)

		journal_entry.set("accounts", [
			{
				"account": dr_account,
				"debit_in_account_currency": payment_amount
			},
			{
				"account": cr_account,
				"credit_in_account_currency": payment_amount
			}
		])
		journal_entry.save(ignore_permissions = True)

		if method == "on_submit":
			frappe.set_value(doc.doctype, doc.name, "journal_name", journal_entry.name)
			msg_to_print = doc.doctype + " journal " + journal_entry.name + " has been created."
		elif method == "on_cancel":
			msg_to_print = doc.doctype + " reverse journal " + journal_entry.name + " has been created."
		frappe.msgprint(msg_to_print)
	if (doc.auto_created_based_on):
		frappe.set_value("Additional Salary", doc.auto_created_based_on, "last_transaction_amount", doc.amount)

@frappe.whitelist()
def generate_additional_salary_records():
	today_date = today()
	auto_repeat_frequency = {
		"Monthly": 1,
		"Annually": 12
	}
	additional_salary_list = frappe.get_all("Additional Salary", filters={"docstatus": "1", "auto_repeat_frequency": ("!=", "None"), "auto_repeat_end_date": ("!=", ""), "auto_repeat_end_date": (">=", today_date)}, fields={"name", "auto_repeat_end_date", "last_transaction_date", "last_transaction_amount", "auto_repeat_frequency", "payroll_date", "employee", "salary_component", "employee_name", "type", "overwrite_salary_structure_amount", "amount"})
	# frappe.msgprint("Additional Salary List lookedup: " + str(additional_salary_list))
	if additional_salary_list:
		# frappe.msgprint("In the salary loop")
		for additional_salary_doc in additional_salary_list:
			#frappe.msgprint("New Additional Salary Doc loaded: ")
			# frappe.msgprint(str(additional_salary_doc.name) + " auto repeat additional salary loaded.")
			# frappe.msgprint(str(additional_salary_doc.auto_repeat_end_date) + " auto repeat end date loaded.")
			# frappe.msgprint(str(additional_salary_doc.last_transaction_date) + " last transaction date loaded.")
			# frappe.msgprint(str(additional_salary_doc.auto_repeat_frequency) + " auto repeat frequency loaded.")
			#additional_salary_doc.last_transaction_date
			if additional_salary_doc.last_transaction_date == None:
				#frappe.msgprint("Blank last transaction date found for " + additional_salary_doc.name + ". Setting payroll date of original transaction")
				additional_salary_doc.last_transaction_date = additional_salary_doc.payroll_date
			if additional_salary_doc.last_transaction_amount == 0:
				# frappe.msgprint("Blank last transaction amount found " + str(additional_salary_doc.last_transaction_amount) + ". Setting amount of original transaction: " + str(additional_salary_doc.amount))
				additional_salary_doc.last_transaction_amount = additional_salary_doc.amount
			if additional_salary_doc.auto_repeat_frequency == "Weekly":
				next_date = add_days(getdate(additional_salary_doc.last_transaction_date), 7)
			else:
				# frappe.msgprint("auto_repeat_frequency must be Monthly or Annually")
				frequency_factor = auto_repeat_frequency.get(additional_salary_doc.auto_repeat_frequency, "Invalid frequency")
				if frequency_factor == "Invalid frequency":
					frappe.throw("Invalid frequency: {0} for {1} not found. Contact the developers!".format(additional_salary_doc.auto_repeat_frequency, additional_salary_doc.name))
				next_date = add_months(getdate(additional_salary_doc.last_transaction_date), frequency_factor)
			# Create 13 days in advance - specificlaly to allow mid salary advance.
			# frappe.msgprint("next date" + str(next_date) + " todays date string " + str(add_days(getdate(today_date), 13)))
			if next_date <= add_days(getdate(today_date), 13):
				additional_salary = frappe.new_doc('Additional Salary')
				additional_salary.employee = additional_salary_doc.employee
				additional_salary.payroll_date = next_date
				additional_salary.salary_component = additional_salary_doc.salary_component
				additional_salary.employee_name = additional_salary_doc.employee_name
				additional_salary.amount = additional_salary_doc.last_transaction_amount
				additional_salary.company = additional_salary_doc.company
				additional_salary.overwrite_salary_structure_amount = additional_salary_doc.overwrite_salary_structure_amount
				additional_salary.type = additional_salary_doc.type
				additional_salary.auto_repeat_frequency = "None"
				additional_salary.auto_created_based_on = additional_salary_doc.name
				additional_salary.auto_repeat_end_date = None
				additional_salary.last_transaction_date = None
				additional_salary.save(ignore_permissions = True)
				frappe.set_value("Additional Salary", additional_salary_doc.name, "last_transaction_date", next_date)
				frappe.msgprint("New additional salary created for " + additional_salary_doc.auto_repeat_frequency + " dated " + str(next_date))
