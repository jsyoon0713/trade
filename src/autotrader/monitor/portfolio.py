"""포트폴리오 수익률 모니터링"""
import logging

from ..broker.ls_broker import LSBroker
from ..broker.models import AccountBalance

logger = logging.getLogger(__name__)


class PortfolioMonitor:
    def __init__(self, broker: LSBroker):
        self.broker = broker

    def get_balance(self) -> AccountBalance:
        return self.broker.get_balance()

    def print_summary(self) -> AccountBalance:
        balance = self.get_balance()
        logger.info("=" * 50)
        logger.info(f"📊 포트폴리오 현황")
        logger.info(f"  총 평가금액 : {balance.total_eval:,.0f}원")
        logger.info(f"  예수금      : {balance.cash:,.0f}원")
        logger.info(
            f"  총 손익     : {balance.total_profit_loss:+,.0f}원 "
            f"({balance.total_profit_pct:+.2f}%)"
        )
        if balance.positions:
            logger.info("  보유 종목:")
            for pos in balance.positions:
                emoji = "📈" if pos.profit_pct >= 0 else "📉"
                logger.info(
                    f"    {emoji} {pos.symbol} {pos.quantity}주 | "
                    f"평균가 {pos.avg_price:,.0f} | 현재가 {pos.current_price:,.0f} | "
                    f"손익 {pos.profit_pct:+.2f}% ({pos.profit_amount:+,.0f}원)"
                )
        else:
            logger.info("  보유 종목 없음")
        logger.info("=" * 50)
        return balance

    def format_report(self) -> str:
        """텔레그램 전송용 포트폴리오 리포트 문자열"""
        balance = self.get_balance()
        lines = [
            "📊 *포트폴리오 현황*",
            f"총 평가금액: `{balance.total_eval:,.0f}원`",
            f"예수금: `{balance.cash:,.0f}원`",
            f"총 손익: `{balance.total_profit_loss:+,.0f}원 ({balance.total_profit_pct:+.2f}%)`",
        ]
        if balance.positions:
            lines.append("\n*보유 종목:*")
            for pos in balance.positions:
                emoji = "📈" if pos.profit_pct >= 0 else "📉"
                lines.append(
                    f"{emoji} {pos.symbol} {pos.quantity}주 | "
                    f"{pos.profit_pct:+.2f}% (`{pos.profit_amount:+,.0f}원`)"
                )
        return "\n".join(lines)
