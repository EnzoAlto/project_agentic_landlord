"""Local Streamlit dashboard for landlord review workflows."""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import streamlit as st

from app.billing import BillingService, MonthlyBill, format_peso
from app.config import Settings
from app.database import Database
from app.maintenance import MaintenanceService, VALID_STATUSES
from app.payments import PaymentService, PaymentSubmission
from app.reminders import ReminderService


PAYMENT_STATUS_LABELS = {
    "awaiting_proof": "Awaiting proof",
    "pending_review": "Pending review",
    "approved": "Approved",
    "rejected": "Rejected",
    "needs_information": "Needs information",
}
MAINTENANCE_STATUS_LABELS = {
    "open": "Open",
    "acknowledged": "Acknowledged",
    "in_progress": "In progress",
    "resolved": "Resolved",
    "closed": "Closed",
}
BILL_STATUS_LABELS = {
    "incomplete": "Incomplete",
    "unpaid": "Unpaid",
    "partially_paid": "Partially paid",
    "paid": "Paid",
    "overdue": "Overdue",
}


def _safe_proof_path(raw_path: str | None, proof_root: Path) -> Path | None:
    if not raw_path:
        return None
    root = proof_root.resolve()
    candidate = Path(raw_path).expanduser().resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _payment_rows(submissions: list[PaymentSubmission]) -> list[dict]:
    return [
        {
            "ID": item.id,
            "Group": item.group_title,
            "Submitted by": item.submitter_name or "Unknown",
            "Items": ", ".join(item.categories) or "Not specified",
            "Claimed period": item.billing_period or "Not specified",
            "Amount received": (
                format_peso(getattr(item, "amount_received_cents", 0))
                if getattr(item, "amount_received_cents", None) is not None
                else "—"
            ),
            "Advance credit": (
                format_peso(getattr(item, "credit_amount_cents", 0))
                if getattr(item, "credit_amount_cents", None)
                else "—"
            ),
            "Date received": getattr(item, "received_date", None) or "—",
            "OR number": getattr(item, "or_number", None) or "—",
            "AR number": getattr(item, "ar_number", None) or "—",
            "Status": PAYMENT_STATUS_LABELS[item.status],
            "Created": item.created_at,
        }
        for item in submissions
    ]


def _bill_label(bill: MonthlyBill) -> str:
    return (
        f"{bill.billing_period} · outstanding "
        f"{format_peso(bill.outstanding_cents)}"
    )


def _render_payment_review(
    submission: PaymentSubmission,
    payments: PaymentService,
    billing: BillingService,
    proof_root: Path,
) -> None:
    with st.container(border=True):
        st.subheader(f"Payment proof #{submission.id}")
        image_column, review_column = st.columns([1, 1.25], gap="large")

        with image_column:
            proof_path = _safe_proof_path(submission.proof_path, proof_root)
            if proof_path:
                st.image(str(proof_path), caption="Submitted payment proof")
            else:
                st.info("No locally available screenshot for this submission.")
            st.markdown(f"**Group:** {submission.group_title}")
            st.markdown(
                f"**Submitted by:** {submission.submitter_name or 'Unknown'}"
            )
            st.markdown(f"**Claim:** {submission.summary}")
            st.markdown(
                "**Claimed items:** "
                + (", ".join(submission.categories) or "Not specified")
            )
            if submission.caption:
                st.markdown(f"**Caption:** {submission.caption}")

        with review_column:
            bills = [
                bill
                for bill in billing.list_bills(submission.group_chat_id)
                if bill.outstanding_cents > 0
            ]
            selected_bill = None
            if bills:
                bill_ids = [bill.id for bill in bills]
                selected_bill_id = st.selectbox(
                    "Apply payment to billing period",
                    bill_ids,
                    format_func=lambda value: _bill_label(
                        next(bill for bill in bills if bill.id == value)
                    ),
                    key=f"payment_bill_{submission.id}",
                )
                selected_bill = next(
                    bill for bill in bills if bill.id == selected_bill_id
                )
                bill_metrics = st.columns(3)
                bill_metrics[0].metric(
                    "Total bill", format_peso(selected_bill.total_due_cents)
                )
                bill_metrics[1].metric(
                    "Already paid", format_peso(selected_bill.allocated_cents)
                )
                bill_metrics[2].metric(
                    "Outstanding", format_peso(selected_bill.outstanding_cents)
                )
            else:
                st.warning(
                    "No unpaid monthly bill is configured for this group. "
                    "Create one in Billing setup before approving the proof."
                )

            received_on = st.date_input(
                "Date received",
                value=date.today(),
                key=f"received_date_{submission.id}",
            )
            receipt_columns = st.columns(2)
            or_number = receipt_columns[0].text_input(
                "OR number (optional)", key=f"or_{submission.id}"
            )
            ar_number = receipt_columns[1].text_input(
                "AR number (optional)", key=f"ar_{submission.id}"
            )
            note = st.text_area(
                "Review note",
                value=submission.review_note or "",
                placeholder="Required when rejecting or requesting details.",
                key=f"payment_note_{submission.id}",
            )

            st.markdown("**Approve payment**")
            full_column, amount_column = st.columns(2)
            if full_column.button(
                "Mark fully paid",
                type="primary",
                width="stretch",
                disabled=selected_bill is None,
                key=f"fully_paid_{submission.id}",
            ):
                result = billing.apply_payment(
                    submission_id=submission.id,
                    bill_id=selected_bill.id,
                    amount_received=None,
                    fully_paid=True,
                    received_date=received_on.isoformat(),
                    or_number=or_number,
                    ar_number=ar_number,
                    note=note,
                )
                st.success(
                    f"Recorded {format_peso(result.amount_received_cents)}. "
                    "Telegram notification queued."
                )
                st.rerun()

            amount_received = amount_column.number_input(
                "Amount received",
                min_value=0.01,
                value=(
                    selected_bill.outstanding_cents / 100
                    if selected_bill is not None
                    else 0.01
                ),
                step=100.0,
                format="%.2f",
                disabled=selected_bill is None,
                key=f"amount_received_{submission.id}",
            )
            if amount_column.button(
                "Record entered amount",
                width="stretch",
                disabled=selected_bill is None,
                key=f"record_amount_{submission.id}",
            ):
                result = billing.apply_payment(
                    submission_id=submission.id,
                    bill_id=selected_bill.id,
                    amount_received=amount_received,
                    fully_paid=False,
                    received_date=received_on.isoformat(),
                    or_number=or_number,
                    ar_number=ar_number,
                    note=note,
                )
                message = f"Applied {format_peso(result.applied_cents)}."
                if result.remaining_cents:
                    message += (
                        f" Remaining: {format_peso(result.remaining_cents)}."
                    )
                if result.credit_cents:
                    message += f" Credit: {format_peso(result.credit_cents)}."
                st.success(message)
                st.rerun()

            st.markdown("**Other review actions**")
            reject_column, details_column = st.columns(2)
            if reject_column.button(
                "Reject",
                width="stretch",
                key=f"reject_{submission.id}",
            ):
                if not note.strip():
                    st.error("Add a reason before rejecting the proof.")
                else:
                    payments.review(
                        submission_id=submission.id,
                        status="rejected",
                        note=note,
                    )
                    st.rerun()
            if details_column.button(
                "Request details",
                width="stretch",
                key=f"details_{submission.id}",
            ):
                if not note.strip():
                    st.error("Describe the information the resident should send.")
                else:
                    payments.review(
                        submission_id=submission.id,
                        status="needs_information",
                        note=note,
                    )
                    st.rerun()


def _render_review_queue(
    payments: PaymentService,
    billing: BillingService,
    proof_root: Path,
) -> None:
    submissions = payments.list_submissions()
    counts = Counter(item.status for item in submissions)
    metrics = st.columns(4)
    metrics[0].metric("Pending review", counts["pending_review"])
    metrics[1].metric("Needs information", counts["needs_information"])
    metrics[2].metric("Approved", counts["approved"])
    metrics[3].metric("All submissions", len(submissions))

    review_queue = [
        item
        for item in submissions
        if item.status in {"pending_review", "needs_information"}
    ]
    if not review_queue:
        st.info("No payment proofs currently need review.")
        return
    for submission in review_queue:
        _render_payment_review(submission, payments, billing, proof_root)


def _render_monthly_ledger(billing: BillingService) -> None:
    bills = billing.list_bills()
    units = billing.list_units()
    total_due = sum(bill.total_due_cents for bill in bills)
    outstanding = sum(bill.outstanding_cents for bill in bills)
    credits = sum(
        billing.available_credit_cents(unit.id)
        for unit in units
    )
    long_overdue = sum(
        bill.outstanding_cents > 0 and bill.days_overdue >= 30 for bill in bills
    )

    metrics = st.columns(4)
    metrics[0].metric("Total billed", format_peso(total_due))
    metrics[1].metric("Outstanding", format_peso(outstanding))
    metrics[2].metric("Available credit", format_peso(credits))
    metrics[3].metric("30+ days overdue", long_overdue)

    if not bills:
        st.info("No monthly bills yet. Create one in Billing setup.")
        return

    filter_columns = st.columns(2)
    periods = sorted({bill.billing_period for bill in bills}, reverse=True)
    period_filter = filter_columns[0].selectbox(
        "Billing period", ["All", *periods]
    )
    status_filter = filter_columns[1].selectbox(
        "Bill status", ["All", *BILL_STATUS_LABELS.values()]
    )
    filtered = bills
    if period_filter != "All":
        filtered = [bill for bill in filtered if bill.billing_period == period_filter]
    if status_filter != "All":
        status_key = next(
            key for key, label in BILL_STATUS_LABELS.items()
            if label == status_filter
        )
        filtered = [bill for bill in filtered if bill.status == status_key]

    rows = []
    for bill in filtered:
        warning = ""
        if bill.days_overdue >= 30:
            warning = f"🔴 {bill.days_overdue} days"
        elif bill.days_overdue:
            warning = f"🟠 {bill.days_overdue} days"
        rows.append(
            {
                "Unit": bill.unit_number,
                "Group": bill.group_title,
                "Period": bill.billing_period,
                "Due": bill.due_date,
                "Rent + parking": format_peso(bill.rent_parking_cents),
                "Water diff": bill.water_difference if bill.water_difference is not None else "—",
                "Water bill": format_peso(bill.water_bill_cents),
                "Electricity diff": (
                    bill.electricity_difference
                    if bill.electricity_difference is not None
                    else "—"
                ),
                "Electricity bill": format_peso(bill.electricity_bill_cents),
                "Total": format_peso(bill.total_due_cents),
                "Paid / credited": format_peso(bill.allocated_cents),
                "Outstanding": format_peso(bill.outstanding_cents),
                "Status": BILL_STATUS_LABELS[bill.status],
                "Overdue": warning,
            }
        )
    st.dataframe(rows, width="stretch", hide_index=True)


def _render_billing_setup(billing: BillingService) -> None:
    st.subheader("Monthly billing setup")
    months = [date(year, month, 1).strftime("%Y-%m")
              for year in range(date.today().year - 2, date.today().year + 4)
              for month in range(1, 13)]
    months = sorted(set(months + [bill.billing_period for bill in billing.list_bills()]))
    period = st.selectbox("Billing month", months,
                          index=months.index(date.today().strftime("%Y-%m")),
                          key="billing_month")
    st.caption("Rent changes apply from this month onward. Utilities apply only to this month. Save changes before switching months.")
    units = billing.list_units()
    groups = billing.list_telegram_groups()
    labels = {int(group["chat_id"]): f"{group['title']} ({group['chat_id']})"
              for group in groups}
    reverse_labels = {label: chat_id for chat_id, label in labels.items()}
    current = {bill.unit_id: bill for bill in billing.list_bills()
               if bill.billing_period == period}
    year, month = map(int, period.split("-"))
    previous_period = (date(year, month, 1) - timedelta(days=1)).strftime("%Y-%m")
    previous = {bill.unit_id: bill for bill in billing.list_bills()
                if bill.billing_period == previous_period}
    rows = []
    for unit in units:
        bill = current.get(unit.id)
        prior = previous.get(unit.id)
        water = bill.water_bill_cents / 100 if bill else 0.0
        electricity = bill.electricity_bill_cents / 100 if bill else 0.0
        rent = bill.rent_parking_cents / 100 if bill else billing.rent_for_period(unit.id, period) / 100
        rows.append({
            "_id": unit.id, "Unit": unit.unit_number, "Name": unit.resident_name,
            "Due day": unit.due_day, "Rent + parking": rent,
            "Water previous": bill.water_previous if bill else prior.water_present if prior else None,
            "Water present": bill.water_present if bill else None,
            "Water difference": bill.water_difference if bill else None,
            "Water bill": water,
            "Electricity previous": bill.electricity_previous if bill else prior.electricity_present if prior else None,
            "Electricity present": bill.electricity_present if bill else None,
            "Electricity difference": bill.electricity_difference if bill else None,
            "Electricity bill": electricity,
            "Total": rent + water + electricity,
            "Bot status": unit.chatbot_status,
            "Telegram group": labels.get(unit.telegram_group_chat_id, "Not connected"),
            "Chat ID": str(unit.telegram_group_chat_id) if unit.telegram_group_chat_id else "—",
        })
    config = {
        "_id": None,
        "Due day": st.column_config.NumberColumn("Due day", min_value=1, max_value=31, step=1),
        "Telegram group": st.column_config.SelectboxColumn(
            "Telegram group", options=["Not connected", *reverse_labels], required=True),
    }
    for column in ("Rent + parking", "Water bill", "Electricity bill", "Total"):
        config[column] = st.column_config.NumberColumn(column, min_value=0.0, format="₱%.2f")
    for column in ("Water previous", "Water present", "Electricity previous", "Electricity present"):
        config[column] = st.column_config.NumberColumn(column, min_value=0.0)
    edited = st.data_editor(rows, column_config=config, hide_index=True,
        disabled=["Unit", "Water difference", "Electricity difference", "Total", "Bot status", "Chat ID"],
        width="stretch", key=f"monthly_editor_{period}")
    edited_rows = edited.to_dict("records") if hasattr(edited, "to_dict") else list(edited)
    st.caption("Blank utility amounts count as ₱0. Calculated columns refresh after saving.")
    with st.container(horizontal=True, wrap=False, gap="small"):
        save_clicked = st.button("Save monthly table", type="primary", width="content")
        alert_clicked = st.button(
            'Send test bill alerts to all known chats', key='test_bill_alerts', width="content"
        )
    st.caption('Sends a payment reminder to all Residents')
    if alert_clicked:
        count = ReminderService(billing.database).queue_reminders(test=True)
        st.success(f'{count} test reminder(s) queued for Telegram delivery.')
    if save_clicked:
        records = []
        for row in edited_rows:
            records.append({
                "id": row["_id"], "resident_name": row["Name"],
                "due_day": row["Due day"], "rent_parking": row["Rent + parking"],
                "telegram_group_chat_id": reverse_labels.get(row["Telegram group"]),
                "water_previous": row["Water previous"], "water_present": row["Water present"],
                "water_bill": row["Water bill"],
                "electricity_previous": row["Electricity previous"],
                "electricity_present": row["Electricity present"],
                "electricity_bill": row["Electricity bill"],
            })
        try:
            billing.save_month_table(period, records)
        except (ValueError, LookupError) as exc:
            st.error(str(exc))
        else:
            st.session_state.pop(f"monthly_editor_{period}", None)
            st.rerun()


def _render_submission_history(payments: PaymentService) -> None:
    submissions = payments.list_submissions()
    selected_status = st.selectbox(
        "Filter payment status",
        ["All", *PAYMENT_STATUS_LABELS.values()],
        key="payment_status_filter",
    )
    filtered = submissions
    if selected_status != "All":
        status_key = next(
            key for key, label in PAYMENT_STATUS_LABELS.items()
            if label == selected_status
        )
        filtered = [item for item in submissions if item.status == status_key]
    if filtered:
        st.dataframe(_payment_rows(filtered), width="stretch", hide_index=True)
    else:
        st.info("No submissions match this filter.")


def _render_payments(
    payments: PaymentService,
    billing: BillingService,
    proof_root: Path,
) -> None:
    review_tab, ledger_tab, setup_tab, history_tab = st.tabs(
        ["Review queue", "Monthly ledger", "Billing setup", "Submission history"]
    )
    with review_tab:
        _render_review_queue(payments, billing, proof_root)
    with ledger_tab:
        _render_monthly_ledger(billing)
    with setup_tab:
        _render_billing_setup(billing)
    with history_tab:
        _render_submission_history(payments)


def _render_maintenance(maintenance: MaintenanceService) -> None:
    tickets = maintenance.list_all()
    counts = Counter(ticket.status for ticket in tickets)
    metric_columns = st.columns(4)
    metric_columns[0].metric("Open", counts["open"])
    metric_columns[1].metric("In progress", counts["in_progress"])
    metric_columns[2].metric("Resolved", counts["resolved"])
    metric_columns[3].metric(
        "Emergency", sum(ticket.urgency == "emergency" for ticket in tickets)
    )

    filter_columns = st.columns(3)
    status_filter = filter_columns[0].selectbox(
        "Status", ["All", *MAINTENANCE_STATUS_LABELS.values()]
    )
    urgency_filter = filter_columns[1].selectbox(
        "Urgency", ["All", "Emergency", "High", "Normal", "Low"]
    )
    group_filter = filter_columns[2].selectbox(
        "Group", ["All", *sorted({ticket.group_title for ticket in tickets})]
    )
    filtered = tickets
    if status_filter != "All":
        status_key = next(
            key for key, label in MAINTENANCE_STATUS_LABELS.items()
            if label == status_filter
        )
        filtered = [ticket for ticket in filtered if ticket.status == status_key]
    if urgency_filter != "All":
        filtered = [
            ticket for ticket in filtered if ticket.urgency == urgency_filter.lower()
        ]
    if group_filter != "All":
        filtered = [ticket for ticket in filtered if ticket.group_title == group_filter]
    if not filtered:
        st.info("No maintenance requests match these filters.")
        return

    st.dataframe(
        [
            {
                "ID": ticket.id,
                "Group": ticket.group_title,
                "Submitted by": ticket.submitter_name or "Unknown",
                "Issue": ticket.description,
                "Urgency": ticket.urgency.title(),
                "Status": MAINTENANCE_STATUS_LABELS[ticket.status],
                "Created": ticket.created_at,
            }
            for ticket in filtered
        ],
        width="stretch",
        hide_index=True,
    )
    st.subheader("Update a request")
    for ticket in filtered:
        with st.expander(
            f"#{ticket.id} · {ticket.group_title} · {ticket.description}"
        ):
            status_values = sorted(VALID_STATUSES)
            selected = st.selectbox(
                "Status",
                status_values,
                index=status_values.index(ticket.status),
                format_func=lambda value: MAINTENANCE_STATUS_LABELS[value],
                key=f"ticket_status_{ticket.id}",
            )
            if st.button(
                "Save status", type="primary", key=f"save_ticket_{ticket.id}"
            ):
                maintenance.update_status(ticket.id, selected, notify_group=True)
                st.rerun()


def main() -> None:
    st.set_page_config(
        page_title="Landlord Operations", page_icon="🏠", layout="wide"
    )
    settings = Settings.from_env(require_token=False)
    database = Database(settings.database_path)
    database.initialize()
    payments = PaymentService(database)
    billing = BillingService(database)
    maintenance = MaintenanceService(database)

    st.title("Landlord operations")
    st.caption("Local review dashboard for Telegram payments and maintenance")
    payments_tab, maintenance_tab = st.tabs(
        ["Payment requests", "Maintenance requests"]
    )
    with payments_tab:
        _render_payments(payments, billing, settings.payment_proof_dir)
    with maintenance_tab:
        _render_maintenance(maintenance)


if __name__ == "__main__":
    main()
