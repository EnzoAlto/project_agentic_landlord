"""Local Streamlit dashboard for landlord review workflows."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
import os
import time
from zoneinfo import ZoneInfo

import streamlit as st
import streamlit.components.v1 as components

from app.billing import BillingService, MonthlyBill, format_peso, money_to_cents
from app.config import Settings
from app.database import Database
from app.maintenance import MaintenanceService
from app.payments import PaymentService, PaymentSubmission
from app.reminders import ReminderService
from app.proof_cleanup import cleanup_reviewed_proofs
from app.remote_services import RemoteStorageError
from app.payment_claims import claimed_items, billing_periods, resident_notes


_proof_updates = components.declare_component(
    'payment_proof_updates',
    path=str(Path(__file__).parent / 'app' / 'proof_updates_component'),
)


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
            "Claimed Items": claimed_items(item),
            "Billing Period": billing_periods(item),
            "Notes from Resident": resident_notes(item),
            "Amount received": (
                format_peso(getattr(item, "amount_received_cents", 0))
                if getattr(item, "amount_received_cents", None) is not None
                else "—"
            ),
            "General advance credit": (
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
            store = getattr(payments, 'store', None)
            proof = (store.proof_bytes(submission.id) if store else
                     _safe_proof_path(submission.proof_path, proof_root))
            if proof:
                st.image(proof if isinstance(proof, bytes) else str(proof), caption="Submitted payment proof")
            else:
                if submission.status == 'pending_review' and not submission.proof_path:
                    st.info('No screenshot is retained. You can correct the payment details and approve again without an image.')
                elif submission.status == 'needs_information' and not submission.proof_path:
                    st.info("Screenshot removed after review. Awaiting a replacement screenshot.")
                else:
                    st.info("No locally available screenshot for this submission.")
            st.markdown(f"**Group:** {submission.group_title}")
            st.markdown(
                f"**Submitted by:** {submission.submitter_name or 'Unknown'}"
            )
            st.markdown('**Claimed Items**')
            st.text(claimed_items(submission))
            st.markdown('**Billing Period**')
            st.text(billing_periods(submission))
            st.markdown('**Notes from Resident**')
            st.text(resident_notes(submission))
            st.caption('Claims describe the resident’s intentions; they do not reserve future months or confirm payment.')

        with review_column:
            linked_unit = next((unit for unit in billing.list_units()
                                if unit.telegram_group_chat_id == submission.group_chat_id), None)
            bills = sorted(
                [bill for bill in billing.list_bills(submission.group_chat_id)
                 if bill.outstanding_cents > 0],
                key=lambda bill: (bill.billing_period, bill.id),
            )
            claimed_periods = {line.get('period') for line in submission.claim_lines if line.get('period')}
            if not claimed_periods and submission.billing_period:
                claimed_periods = {period.strip() for period in submission.billing_period.split(',')}
            selected_bills = []
            if bills:
                st.markdown('**Apply payment to**')
                choices = [{
                    '_id': bill.id,
                    'Select': (bill.billing_period in claimed_periods if claimed_periods else bill.id == bills[0].id),
                    'Month': bill.billing_period,
                    'Due': bill.due_date,
                    'Outstanding': bill.outstanding_cents / 100,
                } for bill in bills]
                chosen = st.data_editor(
                    choices, hide_index=True, width='stretch',
                    column_config={
                        '_id': None,
                        'Select': st.column_config.CheckboxColumn('Select', default=False),
                        'Outstanding': st.column_config.NumberColumn('Outstanding', format='₱%.2f'),
                    },
                    disabled=['Month', 'Due', 'Outstanding'] if submission.status == 'pending_review' else True,
                    key=f'payment_months_{submission.id}',
                )
                chosen_rows = chosen.to_dict('records') if hasattr(chosen, 'to_dict') else list(chosen)
                chosen_ids = {row['_id'] for row in chosen_rows if row['Select']}
                selected_bills = [bill for bill in bills if bill.id in chosen_ids]
                missing = claimed_periods - {bill.billing_period for bill in bills}
                if missing:
                    st.caption('No unpaid bill available for: ' + ', '.join(sorted(missing)) + '. Future unbilled payments remain general credit.')
            elif linked_unit:
                st.info('No unpaid bill is available. Approve the received amount as general advance credit below.')
            else:
                st.warning('Assign this Telegram group to a unit in Billing setup before approving payment.')
            selected_balance = sum(bill.outstanding_cents for bill in selected_bills)
            st.metric('Selected balance', format_peso(selected_balance))
            if any(bill.status == 'incomplete' for bill in selected_bills):
                st.info('Utilities are not fully entered. Displayed balances cover known charges only.')

            received_on = st.date_input(
                "Date received",
                value=date.fromisoformat(submission.received_date) if submission.received_date else date.today(),
                key=f"received_date_{submission.id}",
            )
            receipt_columns = st.columns(2)
            or_number = receipt_columns[0].text_input(
                "OR number (optional)", value=submission.or_number or '', key=f"or_{submission.id}"
            )
            ar_number = receipt_columns[1].text_input(
                "AR number (optional)", value=submission.ar_number or '', key=f"ar_{submission.id}"
            )
            note = st.text_area(
                "Review note",
                value=submission.review_note or "",
                placeholder="Required when rejecting or requesting details.",
                key=f"payment_note_{submission.id}",
            )

            amount_key = f'review_amount_multi_{submission.id}'
            if st.button(
                'Use selected balance', key=f'use_selected_balance_{submission.id}',
                disabled=not selected_bills or submission.status != 'pending_review',
            ):
                st.session_state[amount_key] = selected_balance / 100
            amount_received = st.number_input(
                'Amount received', min_value=0.01,
                value=(submission.previous_approved_amount_cents or selected_balance or 1) / 100,
                step=100.0, format='%.2f',
                disabled=linked_unit is None or submission.status != 'pending_review',
                key=amount_key,
            )
            remainder = money_to_cents(amount_received)
            preview = []
            for bill in selected_bills:
                applied = min(remainder, bill.outstanding_cents)
                preview.append({
                    'Month': bill.billing_period,
                    'Apply': format_peso(applied),
                    'Remaining': format_peso(bill.outstanding_cents - applied),
                })
                remainder -= applied
            if preview:
                st.markdown('**Allocation preview**')
                st.dataframe(preview, hide_index=True, width='stretch')
                st.caption('Payments are applied to selected months, oldest first.')
            if remainder and (selected_bills or not bills):
                st.info(f'General advance credit: {format_peso(remainder)}')
                st.caption('Credit is applied when a new bill is created; it does not reserve a future month.')
            if bills and not selected_bills:
                st.info('Select at least one month to approve this payment.')
            if st.button(
                'Approve payment' if bills else 'Approve as general advance credit',
                type='primary', width='stretch', key=f'approve_multi_{submission.id}',
                disabled=(linked_unit is None or submission.status != 'pending_review'
                          or bool(bills and not selected_bills)),
            ):
                operation = billing.apply_payment if selected_bills else billing.record_advance
                allocation = ({
                    'bill_ids': [bill.id for bill in selected_bills], 'fully_paid': False,
                    'expected_balances': {str(bill.id): bill.outstanding_cents for bill in selected_bills},
                } if selected_bills else {})
                try:
                    result = operation(
                        submission_id=submission.id, **allocation,
                        amount_received=amount_received, received_date=received_on.isoformat(),
                        or_number=or_number, ar_number=ar_number, note=note,
                    )
                except (ValueError, LookupError, RemoteStorageError) as exc:
                    st.error(str(exc))
                else:
                    st.success(f'Applied {format_peso(result.applied_cents)}. General advance credit: {format_peso(result.credit_cents)}. Telegram notification queued.')
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
    st.caption('Incomplete bills show known charges only. Utilities marked “Not yet billed” have not been confirmed as zero.')
    bills = billing.list_bills()
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

    total_due = sum(bill.total_due_cents for bill in filtered)
    outstanding = sum(bill.outstanding_cents for bill in filtered)
    # Credit belongs to a unit, so count it once even when several bills match.
    credits = sum(billing.available_credit_cents(unit_id)
                  for unit_id in {bill.unit_id for bill in filtered})
    long_overdue = sum(
        bill.outstanding_cents > 0 and bill.days_overdue >= 30 for bill in filtered
    )
    metrics = st.columns(4)
    metrics[0].metric("Total billed", format_peso(total_due))
    metrics[1].metric("Outstanding", format_peso(outstanding))
    metrics[2].metric(
        "General advance credit", format_peso(credits),
        help="Current general credit for units in the filtered results, counted once per unit. Credit is not assigned to a billing month.",
    )
    metrics[3].metric("30+ days overdue", long_overdue)
    if not filtered:
        st.info("No bills match the selected filters." if bills else
                "No monthly bills yet. Create one in Billing setup.")
        return

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
                "Water bill": format_peso(bill.water_bill_cents) if bill.water_entered else 'Not yet billed',
                "Electricity bill": format_peso(bill.electricity_bill_cents) if bill.electricity_entered else 'Not yet billed',
                "Others Bill": format_peso(bill.others_bill_cents),
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
    st.caption('Missing occupied-unit bills are created automatically seven days before the month’s earliest due date while the bot is enabled. Utilities remain not yet billed until entered.')
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
        water = (bill.water_bill_cents / 100 if bill.water_entered else None) if bill else 0.0
        electricity = (bill.electricity_bill_cents / 100 if bill.electricity_entered else None) if bill else 0.0
        others = bill.others_bill_cents / 100 if bill else 0.0
        rent = bill.rent_parking_cents / 100 if bill else billing.rent_for_period(unit.id, period) / 100
        rows.append({
            "_id": unit.id, "Unit": unit.unit_number, "Name": unit.resident_name,
            "Due day": unit.due_day, "Rent + parking": rent,
            "Water previous": bill.water_previous if bill else prior.water_present if prior else None,
            "Water present": bill.water_present if bill else None,
            "Water difference": bill.water_difference if bill else None,
            "Water bill": water,
            "Electricity bill": electricity,
            "Others Bill": others,
            "Total": rent + (water or 0) + (electricity or 0) + others,
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
    for column in ("Rent + parking", "Water bill", "Electricity bill", "Others Bill", "Total"):
        config[column] = st.column_config.NumberColumn(column, min_value=0.0, format="₱%.2f")
    for column in ("Water previous", "Water present"):
        config[column] = st.column_config.NumberColumn(column, min_value=0.0)
    edited = st.data_editor(rows, column_config=config, hide_index=True,
        disabled=["Unit", "Water difference", "Total", "Bot status", "Chat ID"],
        width="stretch", key=f"monthly_editor_{period}")
    edited_rows = edited.to_dict("records") if hasattr(edited, "to_dict") else list(edited)
    st.caption("Blank utility amounts mean not yet billed. Enter 0 to confirm a zero charge. Total includes known charges; calculated columns refresh after saving.")
    with st.container(horizontal=True, wrap=False, gap="small"):
        save_clicked = st.button("Save monthly table", type="primary", width="content")
        alert_clicked = st.button(
            'Send test bill alerts to all known chats', key='test_bill_alerts', width="content"
        )
    st.caption('Sends a payment reminder to all Residents')
    if alert_clicked:
        store = getattr(billing, 'store', None)
        count = (store.queue_reminders(test=True) if store else
                 ReminderService(billing.database).queue_reminders(test=True))
        st.success(f'{count} test reminder(s) queued for Telegram delivery.')
    if save_clicked:
        records = []
        for row in edited_rows:
            saved_bill = current.get(row["_id"])
            records.append({
                "id": row["_id"], "resident_name": row["Name"],
                "due_day": row["Due day"], "rent_parking": row["Rent + parking"],
                "telegram_group_chat_id": reverse_labels.get(row["Telegram group"]),
                "water_previous": row["Water previous"], "water_present": row["Water present"],
                "water_bill": row["Water bill"],
                # Preserve historical readings without exposing them in the editor.
                "electricity_previous": saved_bill.electricity_previous if saved_bill else None,
                "electricity_present": saved_bill.electricity_present if saved_bill else None,
                "electricity_bill": row["Electricity bill"],
                "others_bill": row["Others Bill"] or 0,
            })
        try:
            billing.save_month_table(period, records)
        except (ValueError, LookupError) as exc:
            st.error(str(exc))
        else:
            st.session_state.pop(f"monthly_editor_{period}", None)
            st.rerun()


def _undo_history_approval(row_ids, billing) -> None:
    click = st.session_state.get('undo_approval_click')
    if not click or click['label'] != 'Undo':
        return
    index = click['row']
    if not 0 <= index < len(row_ids):
        return
    submission_id = row_ids[index]
    try:
        billing.undo_approval(submission_id)
    except (ValueError, LookupError, RemoteStorageError) as exc:
        st.session_state['payment_undo_feedback'] = ('error', str(exc))
    else:
        st.session_state.pop(f'amount_received_{submission_id}', None)
        st.session_state.pop(f'review_amount_multi_{submission_id}', None)
        st.session_state.pop(f'payment_months_{submission_id}', None)
        st.session_state['payment_undo_feedback'] = (
            'success', f'Payment #{submission_id} is back in Pending review. Correct its details there and approve again; no image is required.',
        )
    st.session_state['payment_undo_rerun'] = True


def _render_submission_history(payments: PaymentService, billing: BillingService) -> None:
    if st.session_state.pop('payment_undo_rerun', False):
        st.rerun()
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
        rows = _payment_rows(filtered)
        for row, item in zip(rows, filtered):
            row['Action'] = 'Undo' if item.status == 'approved' and item.amount_received_cents else None
        st.dataframe(
            rows, width='stretch', hide_index=True,
            column_config={'Action': st.column_config.ButtonColumn(
                'Action', key='undo_approval_click', on_click=_undo_history_approval,
                args=(tuple(item.id for item in filtered), billing),
                help='Reverse an approval and return it to Pending review without restoring the image. Used credit can prevent undo.',
                pinned=True,
            )},
        )
    else:
        st.info("No submissions match this filter.")


@st.fragment
def _render_live_payment_panel(payments, billing, proof_root, panel, live):
    """Only submission panels rerun on pushed events; billing editors stay outside."""
    store = getattr(payments, 'store', None)
    if store:
        store._state = None  # A fragment rerun must not reuse the previous snapshot.
    st.button('Refresh submissions', key=f'refresh_submissions_{panel}')
    if store and live:
        import modal
        config_key = f'proof_updates_config:{store.app_name}:{store.dataset}'
        try:
            config = st.session_state.get(config_key)
            if not config or config['expires'] <= time.time() + 30:
                config = modal.Function.from_name('landlord-telegram', 'dashboard_live_config').remote(store.dataset)
                st.session_state[config_key] = config
            event = _proof_updates(**config, enabled=True, key=f'proof_updates_{panel}', default=None)
            if (event and event.get('status') == 'expired'
                    and event.get('expires') == config['expires']):
                st.session_state.pop(config_key, None)
                st.rerun(scope='fragment')
        except Exception:
            st.warning('Live payment updates are unavailable. Use Refresh submissions.')
    elif store:
        # Rendering disabled disconnects any existing notification stream.
        _proof_updates(enabled=False, key=f'proof_updates_{panel}', default=None)
    if panel == 'review':
        _render_review_queue(payments, billing, proof_root)
    else:
        _render_submission_history(payments, billing)
    st.caption('Last updated: ' + datetime.now(ZoneInfo('Asia/Manila')).strftime('%H:%M:%S Manila'))


def _render_payments(
    payments: PaymentService,
    billing: BillingService,
    proof_root: Path,
) -> None:
    st.session_state.pop('payment_undo_rerun', None)
    feedback = st.session_state.pop('payment_undo_feedback', None)
    if feedback:
        (st.success if feedback[0] == 'success' else st.error)(feedback[1])
    if not getattr(billing, 'store', None):
        cleanup_reviewed_proofs(billing.database, proof_root)
    live = False
    if getattr(payments, 'store', None):
        live = st.toggle(
            'Live payment updates', value=True, key='live_payment_updates',
            help='Refresh submissions after a proof is saved and three quiet seconds pass. '
                 'Keeps a notification connection open; pause to disconnect.',
        )
    review_tab, ledger_tab, setup_tab, history_tab = st.tabs(
        ["Review queue", "Monthly ledger", "Billing setup", "Submission history"]
    )
    with review_tab:
        _render_live_payment_panel(payments, billing, proof_root, 'review', live)
    with ledger_tab:
        _render_monthly_ledger(billing)
    with setup_tab:
        _render_billing_setup(billing)
    with history_tab:
        _render_live_payment_panel(payments, billing, proof_root, 'history', live)


def _render_maintenance(maintenance: MaintenanceService) -> None:
    feedback = st.session_state.pop('maintenance_save_feedback', None)
    if feedback:
        if feedback['saved']:
            st.success(f"Updated {feedback['saved']} request(s). Telegram notifications queued.")
        for error in feedback['errors']:
            st.error(error)
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

    revision = st.session_state.get('maintenance_editor_revision', 0)
    editor_key = f'maintenance_editor_{revision}_{status_filter}_{urgency_filter}_{group_filter}'
    edited = st.data_editor(
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
        column_config={
            'Status': st.column_config.SelectboxColumn(
                'Status', options=list(MAINTENANCE_STATUS_LABELS.values()), required=True,
            ),
        },
        disabled=['ID', 'Group', 'Submitted by', 'Issue', 'Urgency', 'Created'],
        width="stretch", hide_index=True, key=editor_key,
    )
    st.caption('Edit Status in the table, then save changes. Save before changing filters. Saved updates notify the original Telegram group.')
    if st.button('Save changes', type='primary', key='save_maintenance_changes'):
        rows = edited.to_dict('records') if hasattr(edited, 'to_dict') else list(edited)
        original = {ticket.id: ticket for ticket in filtered}
        status_keys = {label: status for status, label in MAINTENANCE_STATUS_LABELS.items()}
        changes = [row for row in rows
                   if row['ID'] in original
                   and row['Status'] != MAINTENANCE_STATUS_LABELS[original[row['ID']].status]]
        if not changes:
            st.info('No status changes to save.')
            return
        saved, errors = 0, []
        for row in changes:
            try:
                status = status_keys.get(row['Status'])
                if status is None:
                    raise ValueError('Select a valid status')
                maintenance.update_status(row['ID'], status, notify_group=True)
            except (ValueError, LookupError, RemoteStorageError) as exc:
                errors.append(f"Request #{row['ID']} was not confirmed saved: {exc}")
            else:
                saved += 1
        st.session_state['maintenance_save_feedback'] = {'saved': saved, 'errors': errors}
        st.session_state['maintenance_editor_revision'] = revision + 1
        st.rerun()


def main() -> None:
    st.set_page_config(
        page_title="Landlord Operations", page_icon="🏠", layout="wide"
    )
    # Read only named top-level secrets. Never bundle a Modal token in GitHub.
    try:
        for key in ('STORAGE_BACKEND', 'MODAL_WORKER_APP', 'MODAL_DATASET',
                    'MODAL_TOKEN_ID', 'MODAL_TOKEN_SECRET'):
            if key in st.secrets:
                os.environ[key] = str(st.secrets[key])
    except FileNotFoundError:
        pass
    settings = Settings.from_env(require_token=False)
    if settings.storage_backend == 'modal':
        from app.remote_services import (
            ModalStore, RemoteBillingService, RemotePaymentService, RemoteMaintenanceService,
        )
        store = ModalStore(settings.modal_worker_app, settings.modal_dataset,
                           st.session_state.setdefault('modal_pending_writes', {}))
        payments = RemotePaymentService(store)
        billing = RemoteBillingService(store)
        maintenance = RemoteMaintenanceService(store)
    else:
        database = Database(settings.database_path)
        database.initialize()
        payments = PaymentService(database)
        billing = BillingService(database)
        maintenance = MaintenanceService(database)

    st.title("Landlord operations")
    st.caption(f"{'Modal shared storage' if settings.storage_backend == 'modal' else 'Local SQLite'} · Telegram payments and maintenance")
    payments_tab, maintenance_tab = st.tabs(
        ["Payment requests", "Maintenance requests"]
    )
    with payments_tab:
        _render_payments(payments, billing, settings.payment_proof_dir)
    with maintenance_tab:
        _render_maintenance(maintenance)


if __name__ == "__main__":
    main()
