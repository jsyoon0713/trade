"""텔레그램 봇 실시간 알림"""
import asyncio
import logging
import os

from telegram import Bot
from telegram.constants import ParseMode

from ..broker.models import Order, OrderSide, OrderStatus

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self):
        token = os.environ.get("TELEGRAM_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not self.chat_id:
            logger.warning("텔레그램 토큰/채팅 ID 미설정 → 알림 비활성화")
            self._bot = None
        else:
            self._bot = Bot(token=token)

    def send(self, message: str) -> None:
        """동기 메서드 (스케줄러에서 호출용)"""
        if not self._bot:
            return
        try:
            asyncio.run(self._send_async(message))
        except Exception as e:
            logger.error(f"텔레그램 전송 실패: {e}")

    async def _send_async(self, message: str) -> None:
        await self._bot.send_message(
            chat_id=self.chat_id,
            text=message,
            parse_mode=ParseMode.MARKDOWN,
        )

    # ── 알림 템플릿 ────────────────────────────────────────────────────────────

    def notify_order(self, order: Order) -> None:
        if order.status != OrderStatus.FILLED:
            return
        emoji = "🟢 매수" if order.side == OrderSide.BUY else "🔴 매도"
        msg = (
            f"{emoji} 체결\n"
            f"종목: `{order.symbol}`\n"
            f"수량: `{order.quantity}주`\n"
            f"가격: `{order.price:,.0f}원`\n"
            f"사유: {order.memo}"
        )
        self.send(msg)

    def notify_portfolio(self, report: str) -> None:
        self.send(report)

    def notify_error(self, message: str) -> None:
        self.send(f"⚠️ *오류 발생*\n{message}")

    def notify_start(self) -> None:
        self.send("🚀 자동매매 봇 시작")

    def notify_stop(self) -> None:
        self.send("⏹️ 자동매매 봇 종료")
