"""Best-effort Monzo feed notifications independent of scheduling state."""

from urllib.parse import quote

import httpx

from app.domain.transfers import TransferExecution, TransferType
from app.observability import get_logger, monzo_error_details
from app.services.monzo import create_feed_item

logger = get_logger(__name__)

FEED_IMAGE_URL = (
    "https://raw.githubusercontent.com/wchr-aun/monzo-scheduler-ui/"
    "refs/heads/main/public/logo.png"
)
SCHEDULER_UI_URL = "https://monzo-scheduler-ui.vercel.app"


async def notify_transfer_result(
    access_token: str,
    values: TransferExecution,
    transfer_id: str,
    *,
    succeeded: bool,
    pot_name: str | None = None,
) -> None:
    is_deposit = values.transfer_type == TransferType.DEPOSIT.value
    action = "deposit" if is_deposit else "withdrawal"
    past_tense_action = "deposited" if is_deposit else "withdrawn"
    amount = _format_gbp(values.amount)
    pot_name = pot_name or "Pot"
    if succeeded:
        title = f"🎉 {amount} {past_tense_action}"
    else:
        title = f"❌ {amount} {action} failed"
    body = f"Balance → {pot_name}" if is_deposit else f"{pot_name} → Balance"

    account_id = quote(values.account_id, safe="")
    pot_id = quote(values.pot_id, safe="")
    try:
        response = await create_feed_item(
            access_token,
            values.account_id,
            title=title,
            image_url=FEED_IMAGE_URL,
            body=body,
            url=(
                None
                if succeeded
                else f"{SCHEDULER_UI_URL}/account/{account_id}/pot/{pot_id}"
            ),
        )
        if response.is_error:
            error_code, error_message = monzo_error_details(response)
            logger.warning(
                "scheduled_transfer_feed_rejected transfer_id=%s "
                "upstream_status=%d monzo_code=%r monzo_message=%r",
                transfer_id,
                response.status_code,
                error_code,
                error_message,
            )
        response.raise_for_status()
    except Exception:
        logger.warning(
            "scheduled_transfer_feed_failed transfer_id=%s",
            transfer_id,
        )


def _format_gbp(amount: int) -> str:
    pounds, pence = divmod(amount, 100)
    return f"£{pounds:,}.{pence:02d}"


def pot_name(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    name = payload.get("name")
    return name if isinstance(name, str) and name else None
